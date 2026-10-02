"""播放会话：单进程内存态，让已保存场景在播放中接纳后续修订。

会话在创建时复制场景当时的完整快照（字幕、分轨、changes、源版本），
游标从 0 毫秒开始，之后支持两种操作：

- 推进（advance）：提交非负整数 time_ms，游标只能不减；返回源版本、
  游标，以及该时刻处于活动区间 [start_ms, end_ms)（左闭右开）的字幕
  及其轨道。
- 事件推进（advance_events）：同样提交非负整数 time_ms 推进游标，
  额外返回“旧游标 < 事件时刻 <= time_ms”窗口内的全部进入（enter）/
  退出（exit）事件——一次推进跨过整条短字幕也不会漏掉。事件按时刻
  升序，同刻先退出后进入，同类按会话字幕顺序。0 毫秒已活动的字幕由
  创建响应的快照确定，不产生进入事件。事件只随本次推进生成，不保存
  历史、不提供重放。
- 接纳（accept）：把场景当前快照并入会话。轨道数不得变；游标前已开始
  （start_ms <= 游标）的字幕，其 id、speaker、起止时间、locked_track
  必须不变，并固定原分轨；其余字幕可改、可删、可新增，但都必须在游标
  之后开始。随后按新快照顺序，以最少换轨与并列规则重算全部分轨。
  冲突或无解即失败且会话保持不变；成功则整体替换会话快照与源版本，
  场景本身不受影响。

会话只存在于本进程内存中，不随重启保留。同一会话上的推进与接纳通过
会话级互斥锁串行化：每次调用要么完整生效，要么完全无效。接纳是一个
完整操作——场景快照的读取（load_record 回调）、已开始字幕校验、分轨
重算与会话替换都在会话锁内完成，因此并发下迟到的接纳请求读到的一定
是轮到它生效时的场景快照，不会把会话回写到进入锁之前读到的旧版本。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .solver import Subtitle, solve
from .storage import SceneRecord


class AcceptConflict(Exception):
    """接纳场景当前快照失败（对应 HTTP 409）。

    原因包括：轨道数变化、已开始字幕被修改/删除、新增或修改的字幕
    未在游标之后开始、固定已开始字幕的原分轨后重算无解。
    """


class CursorRegression(Exception):
    """推进时间小于当前游标（对应 HTTP 422）。"""

    def __init__(self, cursor_ms: int, time_ms: int) -> None:
        self.cursor_ms = cursor_ms
        self.time_ms = time_ms
        super().__init__(
            f"time_ms={time_ms} 小于当前游标 cursor_ms={cursor_ms}，游标只能不减"
        )


class SceneUnavailable(Exception):
    """接纳生效时场景已不存在（对应 HTTP 404）。"""

    def __init__(self, scene_id: str) -> None:
        self.scene_id = scene_id
        super().__init__(f"场景 {scene_id!r} 不存在")


@dataclass
class PlaybackSession:
    """一次播放会话的内存状态；subtitles 与 tracks 按下标对齐。"""

    session_id: str
    scene_id: str
    source_revision: int
    track_count: int
    subtitles: List[Dict[str, Any]]
    tracks: List[int]
    changes: int
    cursor_ms: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def active(self) -> List[Dict[str, Any]]:
        """游标时刻处于 [start_ms, end_ms) 内的字幕及其轨道（左闭右开）。"""
        return [
            {**sub, "track": track}
            for sub, track in zip(self.subtitles, self.tracks)
            if sub["start_ms"] <= self.cursor_ms < sub["end_ms"]
        ]

    def state(self) -> Dict[str, Any]:
        """会话完整快照（纯数据，可直接作为响应体）。"""
        return {
            "session_id": self.session_id,
            "scene_id": self.scene_id,
            "source_revision": self.source_revision,
            "cursor_ms": self.cursor_ms,
            "track_count": self.track_count,
            "changes": self.changes,
            "subtitles": [dict(s) for s in self.subtitles],
            "assignments": [
                {"id": s["id"], "track": t}
                for s, t in zip(self.subtitles, self.tracks)
            ],
        }


def _identity(sub: Dict[str, Any]) -> Tuple[Any, ...]:
    """已开始字幕在接纳时必须保持不变的字段组合。"""
    return (
        sub["id"],
        sub["speaker"],
        sub["start_ms"],
        sub["end_ms"],
        sub.get("locked_track"),
    )


class PlaybackManager:
    """会话注册表：单进程内存实现，不持久化、不跨重启保留。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, PlaybackSession] = {}
        self._registry_lock = threading.Lock()

    def create(self, record: SceneRecord) -> Dict[str, Any]:
        """按场景当前快照创建会话：复制完整快照，游标从 0 毫秒开始。"""
        subtitles = [dict(s) for s in record.subtitles]
        track_by_id = {a["id"]: a["track"] for a in record.assignments}
        session = PlaybackSession(
            session_id=uuid.uuid4().hex,
            scene_id=record.scene_id,
            source_revision=record.revision,
            track_count=record.track_count,
            subtitles=subtitles,
            tracks=[track_by_id[s["id"]] for s in subtitles],
            changes=record.changes,
        )
        state = session.state()
        with self._registry_lock:
            self._sessions[session.session_id] = session
        return state

    def get(self, session_id: str) -> Optional[PlaybackSession]:
        with self._registry_lock:
            return self._sessions.get(session_id)

    def advance(self, session: PlaybackSession, time_ms: int) -> Dict[str, Any]:
        """推进游标（只能不减），返回源版本、游标与活动字幕及轨道。"""
        with session.lock:
            if time_ms < session.cursor_ms:
                raise CursorRegression(session.cursor_ms, time_ms)
            session.cursor_ms = time_ms
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "cursor_ms": session.cursor_ms,
                "active": session.active(),
            }

    def advance_events(
        self, session: PlaybackSession, time_ms: int
    ) -> Dict[str, Any]:
        """推进游标并返回本次跨过的全部进入/退出事件。

        事件窗口为（旧游标, time_ms]：旧游标 < 事件时刻 <= time_ms。
        字幕在 start_ms 进入、end_ms 退出（区间左闭右开）；因此一次
        采样间隔内开始又结束的短字幕会同时给出退出与进入事件，不会漏。
        0 毫秒已活动（start_ms == 0）的字幕由创建会话时的快照确定，
        初始游标即 0，不产生进入事件。事件按时刻升序；同刻先退出后
        进入；同类按会话字幕顺序。time_ms 等于当前游标时事件为空。

        事件生成、游标更新与活动字幕计算基于同一份会话快照，在会话锁
        内一次完成，与推进、接纳完整串行。倒退抛 CursorRegression，
        会话保持不变。事件不保存历史、不提供重放。
        """
        with session.lock:
            if time_ms < session.cursor_ms:
                raise CursorRegression(session.cursor_ms, time_ms)
            old_cursor = session.cursor_ms
            # 每项为 (时刻, 同刻次序, 会话顺序, 事件体)；同刻次序令 exit 在前
            pending: List[Tuple[int, int, int, Dict[str, Any]]] = []
            for order, (sub, track) in enumerate(
                zip(session.subtitles, session.tracks)
            ):
                start_ms = sub["start_ms"]
                end_ms = sub["end_ms"]
                base = {
                    "id": sub["id"],
                    "speaker": sub["speaker"],
                    "track": track,
                }
                if old_cursor < start_ms <= time_ms:
                    pending.append(
                        (start_ms, 1, order, {**base, "time_ms": start_ms, "type": "enter"})
                    )
                if old_cursor < end_ms <= time_ms:
                    pending.append(
                        (end_ms, 0, order, {**base, "time_ms": end_ms, "type": "exit"})
                    )
            pending.sort(key=lambda item: (item[0], item[1], item[2]))
            events = [item[3] for item in pending]
            session.cursor_ms = time_ms
            return {
                "session_id": session.session_id,
                "scene_id": session.scene_id,
                "source_revision": session.source_revision,
                "cursor_ms": session.cursor_ms,
                "events": events,
                "active": session.active(),
            }

    def accept(
        self,
        session: PlaybackSession,
        load_record: Callable[[], Optional[SceneRecord]],
    ) -> Dict[str, Any]:
        """接纳场景当前快照：读取场景、校验、重算分轨、替换会话为完整操作。

        load_record 在会话锁内调用，因此读到的是“轮到本次接纳生效时”的
        场景快照；与同一会话的推进/接纳串行。这样并发下迟到的请求不会
        把进入锁之前读到的旧快照回写到会话上。场景不存在抛
        SceneUnavailable；冲突或无解抛 AcceptConflict，会话保持不变。
        """
        with session.lock:
            record = load_record()
            if record is None:
                raise SceneUnavailable(session.scene_id)
            if record.track_count != session.track_count:
                raise AcceptConflict(
                    f"轨道数不得变化：会话为 {session.track_count}，"
                    f"场景当前为 {record.track_count}"
                )

            # 游标前已开始（start_ms <= 游标）的字幕必须原样保留
            started = {
                s["id"]: s
                for s in session.subtitles
                if s["start_ms"] <= session.cursor_ms
            }
            new_by_id = {s["id"]: s for s in record.subtitles}
            for sid, old in started.items():
                new = new_by_id.get(sid)
                if new is None:
                    raise AcceptConflict(f"字幕 {sid!r} 已开始，不得删除")
                if _identity(new) != _identity(old):
                    raise AcceptConflict(
                        f"字幕 {sid!r} 已开始，其 id/speaker/起止时间/"
                        "locked_track 不得修改"
                    )
            # 其余字幕可改删或加入，但都必须在游标之后开始
            for new in record.subtitles:
                if new["start_ms"] <= session.cursor_ms and new["id"] not in started:
                    raise AcceptConflict(
                        f"字幕 {new['id']!r} 的开始时间不晚于游标 "
                        f"{session.cursor_ms}：新增/修改的字幕必须在游标之后开始"
                    )

            # 固定已开始字幕的原分轨，按新快照顺序与最少换轨、并列规则重算
            session_track = {
                s["id"]: t for s, t in zip(session.subtitles, session.tracks)
            }
            subs = [
                Subtitle(
                    id=s["id"],
                    speaker=s["speaker"],
                    start_ms=s["start_ms"],
                    end_ms=s["end_ms"],
                    locked_track=(
                        session_track[s["id"]]
                        if s["id"] in started
                        else s.get("locked_track")
                    ),
                )
                for s in record.subtitles
            ]
            tracks, changes = solve(subs, session.track_count)
            if tracks is None:
                raise AcceptConflict("固定已开始字幕的原分轨后，新快照无解")

            # 成功：整体替换会话快照与源版本（场景本身不变）
            session.subtitles = [dict(s) for s in record.subtitles]
            session.tracks = list(tracks)
            session.changes = changes
            session.source_revision = record.revision
            return session.state()
