"""场景持久化：本地 SQLite。

一个场景（scene）保存某次成功修订的完整快照：
字幕输入、分轨结果、换轨次数与版本号（revision）。

并发语义采用乐观锁：客户端提交 expected_revision，新场景填 0，
已存在场景须填写当前版本；服务端在一个 BEGIN IMMEDIATE 事务中
读取当前版本并写入新版本，因此同一版本的并发提交最多一次成功，
其余全部以 RevisionConflict 失败且不改动已有数据。

片段另存（commit_clip）同样在单个 BEGIN IMMEDIATE 事务中读取源
场景的完整快照（字幕、分轨、track_count 同属一次提交）并核对版本，
随后才在该快照上构造片段、占用目标并写入：裁剪数据必然取自与
expected_revision 一致的源快照，读取之后到提交之间源版本不可能
变化，版本不符在读取时即以 RevisionConflict 失败。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_DB_PATH = os.environ.get("SCENES_DB_PATH", "scenes.db")


class RevisionConflict(Exception):
    """expected_revision 与库中当前版本不一致。"""

    def __init__(self, scene_id: str, current_revision: int) -> None:
        self.scene_id = scene_id
        self.current_revision = current_revision
        super().__init__(
            f"场景 {scene_id!r} 的版本冲突：期望新版本基于 "
            f"revision={current_revision} 再次提交"
        )


class TargetSceneExists(Exception):
    """裁剪目标 scene_id 已被占用（含目标与源同名）。"""

    def __init__(self, scene_id: str, current_revision: int) -> None:
        self.scene_id = scene_id
        self.current_revision = current_revision
        super().__init__(
            f"目标场景 {scene_id!r} 已存在（revision={current_revision}），"
            "裁剪目标必须使用未占用的 scene_id"
        )


class SourceSceneMissing(Exception):
    """提交裁剪事务时发现源场景不存在（正常流程中场景不可删除，仅作防御）。"""

    def __init__(self, scene_id: str) -> None:
        self.scene_id = scene_id
        super().__init__(f"源场景 {scene_id!r} 不存在")


@dataclass
class SceneRecord:
    scene_id: str
    revision: int
    track_count: int
    subtitles: List[Dict[str, Any]]
    assignments: List[Dict[str, Any]]
    changes: int


@dataclass
class ClipPayload:
    """裁剪事务内依据与 expected_revision 一致的源快照构造出的片段内容。"""

    track_count: int
    subtitles: List[Dict[str, Any]]
    assignments: List[Dict[str, Any]]
    changes: int


class SceneStore:
    """SQLite 场景存储。线程安全：每次操作使用独立连接（check_same_thread=False）。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL 允许读写并发；busy_timeout 在写锁竞争时等待而非立即报错
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scenes (
                    scene_id      TEXT PRIMARY KEY,
                    revision      INTEGER NOT NULL,
                    track_count   INTEGER NOT NULL,
                    subtitles     TEXT    NOT NULL,
                    assignments   TEXT    NOT NULL,
                    changes       INTEGER NOT NULL,
                    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
                """
            )

    def commit_revision(
        self,
        scene_id: str,
        expected_revision: int,
        track_count: int,
        subtitles: List[Dict[str, Any]],
        assignments: List[Dict[str, Any]],
        changes: int,
    ) -> int:
        """以乐观锁写入一次成功修订，返回新版本号。

        新场景（expected_revision == 0 且库中不存在）写入 revision=1；
        已存在场景仅当 expected_revision == 当前 revision 时覆盖为 +1；
        否则抛出 RevisionConflict，且不改动任何已有数据。
        """
        conn = self._connect()
        try:
            # IMMEDIATE 立即获取保留写锁，串行化并发提交：
            # 同一 expected_revision 的并发提交只有第一个能通过版本核对。
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM scenes WHERE scene_id = ?", (scene_id,)
            ).fetchone()

            if row is None:
                if expected_revision != 0:
                    raise RevisionConflict(scene_id, 0)
                new_revision = 1
                conn.execute(
                    """
                    INSERT INTO scenes
                        (scene_id, revision, track_count, subtitles, assignments, changes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scene_id,
                        new_revision,
                        track_count,
                        json.dumps(subtitles, ensure_ascii=False),
                        json.dumps(assignments, ensure_ascii=False),
                        changes,
                    ),
                )
            else:
                current_revision = row["revision"]
                if expected_revision != current_revision:
                    raise RevisionConflict(scene_id, current_revision)
                new_revision = current_revision + 1
                conn.execute(
                    """
                    UPDATE scenes
                       SET revision = ?, track_count = ?, subtitles = ?,
                           assignments = ?, changes = ?,
                           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                     WHERE scene_id = ?
                    """,
                    (
                        new_revision,
                        track_count,
                        json.dumps(subtitles, ensure_ascii=False),
                        json.dumps(assignments, ensure_ascii=False),
                        changes,
                        scene_id,
                    ),
                )
            conn.commit()
            return new_revision
        except RevisionConflict:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def commit_clip(
        self,
        source_scene_id: str,
        target_scene_id: str,
        expected_revision: int,
        build_payload: Callable[[SceneRecord], ClipPayload],
    ) -> SceneRecord:
        """在单个 BEGIN IMMEDIATE 事务内完成裁剪另存的全部步骤。

        片段始终以 revision=1 作为全新场景写入。事务内依次完成：
        1. 读取源场景完整快照（字幕、分轨、track_count、changes），
           源场景必须存在且 revision == expected_revision；
        2. 在该快照上调用 build_payload 构造片段（时间归零、源分轨
           写入 locked_track、按源 track_count 重算），空窗口由其
           抛 EmptyClip（在 main 中映射为 422）；
        3. target_scene_id 必须尚未被任何场景占用（包括源/目标同名）；
        4. INSERT 目标片段。

        裁剪所用的 track_count、字幕与分轨全部取自与 expected_revision
        一致的同一次源行读取，且该读取处于保留写锁内：源版本不符直接
        RevisionConflict，读取之后直到提交源版本不可能变化。任一条件
        不满足都在提交前抛错回滚：失败不会留下目标，也不会改动源场景。
        返回已提交的目标场景记录（revision=1）。
        """
        conn = self._connect()
        try:
            # IMMEDIATE 立即获取保留写锁：源快照读取、版本核对、目标占用
            # 检查与片段写入在同一写事务内完成，源修订/其他裁剪无法插入
            # “读到源、提交目标”之间的窗口。
            conn.execute("BEGIN IMMEDIATE")
            source_row = _fetch_scene_row(conn, source_scene_id)
            if source_row is None:
                raise SourceSceneMissing(source_scene_id)
            if expected_revision != source_row["revision"]:
                raise RevisionConflict(
                    source_scene_id, source_row["revision"]
                )
            source_record = _row_to_scene_record(source_row)

            payload = build_payload(source_record)

            target_row = _fetch_scene_row(conn, target_scene_id)
            if target_row is not None:
                raise TargetSceneExists(
                    target_scene_id, target_row["revision"]
                )

            conn.execute(
                """
                INSERT INTO scenes
                    (scene_id, revision, track_count, subtitles, assignments, changes)
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (
                    target_scene_id,
                    payload.track_count,
                    json.dumps(payload.subtitles, ensure_ascii=False),
                    json.dumps(payload.assignments, ensure_ascii=False),
                    payload.changes,
                ),
            )
            conn.commit()
            return SceneRecord(
                scene_id=target_scene_id,
                revision=1,
                track_count=payload.track_count,
                subtitles=[dict(s) for s in payload.subtitles],
                assignments=[dict(a) for a in payload.assignments],
                changes=payload.changes,
            )
        except (RevisionConflict, TargetSceneExists, SourceSceneMissing):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_scene(self, scene_id: str) -> Optional[SceneRecord]:
        """按 scene_id 读取最近一次成功修订；不存在返回 None。

        单行单行快照读取（WAL 下一条 SELECT 对同一行取一致版本），
        不会读到某次提交的部分字段。
        """
        with self._connect() as conn:
            row = _fetch_scene_row(conn, scene_id)
        if row is None:
            return None
        return _row_to_scene_record(row)


_SCENE_COLUMNS = (
    "scene_id, revision, track_count, subtitles, assignments, changes"
)


def _fetch_scene_row(
    conn: sqlite3.Connection, scene_id: str
) -> Optional[sqlite3.Row]:
    """在给定连接上读取场景整行（字幕与分轨 JSON 同源、同次提交）。"""
    return conn.execute(
        f"""
        SELECT {_SCENE_COLUMNS}
          FROM scenes WHERE scene_id = ?
        """,
        (scene_id,),
    ).fetchone()


def _row_to_scene_record(row: sqlite3.Row) -> SceneRecord:
    """把场景整行（同源字幕/分轨 JSON）反序列化为 SceneRecord。"""
    subtitles, assignments = _loads_pair(row["subtitles"], row["assignments"])
    return SceneRecord(
        scene_id=row["scene_id"],
        revision=row["revision"],
        track_count=row["track_count"],
        subtitles=subtitles,
        assignments=assignments,
        changes=row["changes"],
    )


def _loads_pair(subtitles_json: str, assignments_json: str) -> Tuple[List, List]:
    """同源反序列化，保证读取到的字幕与分轨属于同一次提交。"""
    subtitles: List[Dict[str, Any]] = json.loads(subtitles_json)
    assignments: List[Dict[str, Any]] = json.loads(assignments_json)
    return subtitles, assignments
