"""FastAPI 入口：请求/响应模型与路由。"""
from __future__ import annotations

from threading import Lock
from typing import List, Literal, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from . import storage as storage_module
from .playback import (
    AcceptConflict,
    CursorRegression,
    PlaybackManager,
    PlaybackSession,
    SceneUnavailable,
)
from .solver import Subtitle, solve, validate
from .storage import (
    RevisionConflict,
    SceneRecord,
    SceneStore,
    SourceSceneMissing,
    TargetSceneExists,
)

app = FastAPI(
    title="对白字幕分轨 API",
    description=(
        "为离线对白字幕分配屏幕轨道：时间重叠的字幕不同轨，"
        "同一说话人按时间相邻字幕的换轨次数最少，并列取轨道号序列字典序最小者。"
        "场景（scene）可按 scene_id 保存修订快照，并以版本号做乐观并发控制；"
        "已保存场景的指定时段可另存为从 0 毫秒起、保持原画面轨道的独立片段。"
        "播放会话（session）按 scene_id 创建内存态会话，游标推进中可接纳场景"
        "后续修订：已开始字幕保持原分轨，其余按原规则重算。事件推进接口返回"
        "一次推进跨过的全部字幕进入/退出事件，短字幕不会在两次采样之间漏掉。"
    ),
    version="1.4.0",
)


class SubtitleIn(BaseModel):
    id: Union[str, int] = Field(..., description="全局唯一的字幕 id")
    speaker: str = Field(..., description="说话人标识，非空")
    start_ms: int = Field(..., ge=0, description="开始时间（毫秒，非负）")
    end_ms: int = Field(..., ge=0, description="结束时间（毫秒，必须大于 start_ms）")
    locked_track: Optional[int] = Field(
        default=None, ge=1, description="可选：锁定轨道号（从 1 开始）"
    )

    @field_validator("speaker")
    @classmethod
    def _speaker_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("speaker 不能为空")
        return v

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, v: Union[str, int]) -> Union[str, int]:
        if isinstance(v, str) and v == "":
            raise ValueError("id 不能为空字符串")
        return v

    @model_validator(mode="after")
    def _check_interval(self) -> "SubtitleIn":
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms 必须小于 end_ms")
        return self


class AssignRequest(BaseModel):
    track_count: int = Field(
        ..., ge=2, le=3, description="屏幕轨道数（2～3，轨道号从 1 开始）"
    )
    subtitles: List[SubtitleIn] = Field(
        ..., min_length=1, max_length=12, description="字幕列表（1～12 条，按输入顺序）"
    )

    @model_validator(mode="after")
    def _check_cross_item_rules(self) -> "AssignRequest":
        subs = [
            Subtitle(
                id=s.id,
                speaker=s.speaker,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                locked_track=s.locked_track,
            )
            for s in self.subtitles
        ]
        error = validate(subs, self.track_count)
        if error is not None:
            raise ValueError(error)
        return self


class SceneRevisionRequest(AssignRequest):
    """场景修订请求：沿用 /assign 的全部字段与校验，另带版本号。

    expected_revision：客户端所依据的当前版本。新场景填 0；
    已存在场景必须填写当前版本，成功后版本 +1。
    """

    expected_revision: int = Field(
        ..., ge=0, description="所依据的当前场景版本；新场景填 0"
    )


class ClipRequest(BaseModel):
    """片段另存请求：把已保存场景的 [from_ms, to_ms) 另存为新场景。

    时间字段使用严格整数（strict）：布尔值（true/false 会被 Python 视为
    int 子类）与数字字符串一律按 422 拒绝，不做隐式转换。
    """

    target_scene_id: str = Field(
        ..., description="片段的新场景 id：非空且尚未被占用"
    )
    expected_revision: int = Field(
        ..., ge=0, description="所依据的源场景当前版本"
    )
    from_ms: int = Field(
        ...,
        ge=0,
        strict=True,
        description="裁剪起点（毫秒，非负整数，拒绝布尔值与字符串）",
    )
    to_ms: int = Field(
        ...,
        ge=0,
        strict=True,
        description="裁剪终点（毫秒，非负整数且严格大于 from_ms）",
    )

    @field_validator("target_scene_id")
    @classmethod
    def _target_scene_id_not_blank(cls, v: str) -> str:
        if v.strip() == "":
            raise ValueError("target_scene_id 不能为空")
        return v

    @model_validator(mode="after")
    def _check_interval(self) -> "ClipRequest":
        if self.from_ms >= self.to_ms:
            raise ValueError("from_ms 必须小于 to_ms")
        return self


class TrackAssignment(BaseModel):
    id: Union[str, int]
    track: int


class AssignResponse(BaseModel):
    feasible: bool
    changes: int = Field(..., description="同一说话人按时间相邻字幕的换轨总数")
    assignments: List[TrackAssignment]


class SubtitleOut(BaseModel):
    id: Union[str, int]
    speaker: str
    start_ms: int
    end_ms: int
    locked_track: Optional[int] = None


class SceneResponse(BaseModel):
    """一次成功修订保存后的完整场景快照。"""

    scene_id: str
    revision: int = Field(..., description="本次提交成功后的新版本号")
    track_count: int
    subtitles: List[SubtitleOut]
    changes: int
    assignments: List[TrackAssignment]
    feasible: bool = True


# ---------------------------------------------------------------------------
# 播放会话：请求/响应模型
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    scene_id: str = Field(..., description="要播放的场景 id")


class SessionStateResponse(BaseModel):
    """会话完整状态：创建与接纳成功后返回。"""

    session_id: str
    scene_id: str
    source_revision: int = Field(..., description="会话快照来源的场景版本")
    cursor_ms: int = Field(..., description="播放游标（毫秒）")
    track_count: int
    changes: int
    subtitles: List[SubtitleOut]
    assignments: List[TrackAssignment]


class ActiveSubtitleOut(BaseModel):
    id: Union[str, int]
    speaker: str
    start_ms: int
    end_ms: int
    locked_track: Optional[int] = None
    track: int


class AdvanceRequest(BaseModel):
    time_ms: int = Field(
        ..., ge=0, description="推进到的时刻（毫秒，非负整数，只能不减）"
    )


class AdvanceResponse(BaseModel):
    """推进结果：源版本、游标，以及游标时刻 [start_ms, end_ms) 内的字幕和轨道。"""

    session_id: str
    scene_id: str
    source_revision: int
    cursor_ms: int
    active: List[ActiveSubtitleOut]


class SubtitleEventOut(BaseModel):
    """一次推进跨过的单条字幕事件：start_ms 进入，end_ms 退出。"""

    id: Union[str, int]
    speaker: str
    track: int
    time_ms: int = Field(..., description="事件时刻（毫秒）：进入取 start_ms，退出取 end_ms")
    type: Literal["enter", "exit"] = Field(
        ..., description="进入（enter）或退出（exit）"
    )


class AdvanceEventsResponse(BaseModel):
    """事件推进结果：本次跨过的事件、推进后的活动字幕、源版本与游标。

    事件窗口为（旧游标, time_ms]，按时刻升序，同刻先退出后进入，
    同类按会话字幕顺序；等时推进事件为空。
    """

    session_id: str
    scene_id: str
    source_revision: int
    cursor_ms: int
    events: List[SubtitleEventOut]
    active: List[ActiveSubtitleOut]


# ---------------------------------------------------------------------------
# SQLite 存储：惰性单例，路径可用环境变量 SCENES_DB_PATH 覆盖
# ---------------------------------------------------------------------------

_store: Optional[SceneStore] = None
_store_lock = Lock()


def get_store() -> SceneStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SceneStore(storage_module.DEFAULT_DB_PATH)
    return _store


def reset_store() -> None:
    """重置单例（供测试重新指定 SCENES_DB_PATH 后使用）。"""
    global _store
    with _store_lock:
        _store = None


# ---------------------------------------------------------------------------
# 播放会话管理器：单进程内存，不持久化、不跨重启保留
# ---------------------------------------------------------------------------

_playback: Optional[PlaybackManager] = None
_playback_lock = Lock()


def get_playback() -> PlaybackManager:
    global _playback
    if _playback is None:
        with _playback_lock:
            if _playback is None:
                _playback = PlaybackManager()
    return _playback


def reset_playback() -> None:
    """清空全部播放会话（供测试隔离；会话本就不跨重启保留）。"""
    global _playback
    with _playback_lock:
        _playback = None


def _subtitles_from_request(req: AssignRequest) -> List[Subtitle]:
    return [
        Subtitle(
            id=s.id,
            speaker=s.speaker,
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            locked_track=s.locked_track,
        )
        for s in req.subtitles
    ]


def _check_scene_id(scene_id: str) -> None:
    if scene_id.strip() == "":
        raise HTTPException(status_code=422, detail="scene_id 不能为空")


class EmptyClip(Exception):
    """裁剪窗口与源场景没有任何相交字幕（对应 HTTP 422）。"""


def _build_clip(
    record: SceneRecord, from_ms: int, to_ms: int
) -> tuple[List[Subtitle], List[int], int]:
    """构造 [from_ms, to_ms) 片段：保留相交字幕，保持源画面轨道，时间归零。

    - 仅保留与 [from_ms, to_ms) 左闭右开区间相交的字幕（源顺序不变）；
    - 起止时间裁到窗口内再整体减去 from_ms，片段时间轴从 0 毫秒起；
    - id、speaker 沿用源字幕；源场景为该 id 求出的分轨写入 locked_track，
      再以源 track_count 按既有最少换轨、并列规则重算——由于源分轨互相
      不冲突且锁轨与源分轨一致，求解结果必然等于源画面轨道；
    - 窗口内没有相交字幕时抛 EmptyClip。
    """
    track_by_id = {a["id"]: a["track"] for a in record.assignments}
    subs: List[Subtitle] = []
    for s in record.subtitles:
        start_ms, end_ms = s["start_ms"], s["end_ms"]
        # 左闭右开：start < to 且 end > from 才相交
        if start_ms >= to_ms or end_ms <= from_ms:
            continue
        subs.append(
            Subtitle(
                id=s["id"],
                speaker=s["speaker"],
                start_ms=max(start_ms, from_ms) - from_ms,
                end_ms=min(end_ms, to_ms) - from_ms,
                locked_track=track_by_id[s["id"]],
            )
        )
    if not subs:
        raise EmptyClip()
    tracks, changes = solve(subs, record.track_count)
    if tracks is None:  # 理论不可达：源分轨本身即是合法解
        raise RuntimeError("裁剪片段在固定源分轨后无解")
    return subs, tracks, changes


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/assign", response_model=AssignResponse)
def assign(req: AssignRequest) -> AssignResponse:
    subs = _subtitles_from_request(req)
    tracks, changes = solve(subs, req.track_count)
    if tracks is None:
        # 锁定冲突 / 轨道不足：无解，方案为空（HTTP 仍为 200；非法输入才是 422）
        return AssignResponse(feasible=False, changes=0, assignments=[])
    return AssignResponse(
        feasible=True,
        changes=changes,
        assignments=[
            TrackAssignment(id=s.id, track=t) for s, t in zip(subs, tracks)
        ],
    )


@app.put(
    "/scenes/{scene_id}",
    response_model=Union[SceneResponse, AssignResponse],
)
def revise_scene(
    scene_id: str, req: SceneRevisionRequest
) -> Union[SceneResponse, AssignResponse]:
    """创建或修订场景。

    - 新场景：expected_revision=0，成功后 revision=1；
    - 修订：expected_revision 必须等于当前版本，成功后版本 +1；
    - 合法但无解：沿用 /assign 的无解响应（feasible=false），不保存、不改版本；
    - 非法输入：422，不改变已有版本；
    - 版本不符（含同一版本的并发竞争落败方）：409，不改变已有版本。
    """
    _check_scene_id(scene_id)

    # 校验由 Pydantic 模型在进入本函数前完成（非法 -> 422）。
    # 求解沿用 /assign 的同一套校验、最少换轨与并列规则。
    subs = _subtitles_from_request(req)
    tracks, changes = solve(subs, req.track_count)
    if tracks is None:
        # 合法但无解：与 /assign 完全相同的响应，且不落库
        return AssignResponse(feasible=False, changes=0, assignments=[])

    subtitles_data = [s.model_dump() for s in req.subtitles]
    assignments_data = [
        {"id": s.id, "track": t} for s, t in zip(subs, tracks)
    ]

    try:
        new_revision = get_store().commit_revision(
            scene_id=scene_id,
            expected_revision=req.expected_revision,
            track_count=req.track_count,
            subtitles=subtitles_data,
            assignments=assignments_data,
            changes=changes,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )

    return SceneResponse(
        scene_id=scene_id,
        revision=new_revision,
        track_count=req.track_count,
        subtitles=[s.model_dump() for s in req.subtitles],
        changes=changes,
        assignments=[
            TrackAssignment(id=a["id"], track=a["track"]) for a in assignments_data
        ],
    )


def _record_to_scene_response(record: SceneRecord) -> SceneResponse:
    return SceneResponse(
        scene_id=record.scene_id,
        revision=record.revision,
        track_count=record.track_count,
        subtitles=record.subtitles,
        changes=record.changes,
        assignments=record.assignments,
    )


@app.get("/scenes/{scene_id}", response_model=SceneResponse)
def read_scene(scene_id: str) -> SceneResponse:
    """读取场景最近一次成功修订的完整快照；不存在返回 404。"""
    _check_scene_id(scene_id)
    record = get_store().get_scene(scene_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"场景 {scene_id!r} 不存在")
    return _record_to_scene_response(record)


@app.post("/scenes/{scene_id}/clip", response_model=SceneResponse)
def clip_scene(scene_id: str, req: ClipRequest) -> SceneResponse:
    """把已保存场景的 [from_ms, to_ms) 另存为从 0 毫秒起的独立片段。

    保留与窗口相交（左闭右开）的字幕，源顺序不变；起止时间裁到窗口内
    再减去 from_ms；id、speaker 沿用源字幕，源分轨写入各条 locked_track，
    目标沿用源 track_count 与既有求解规则，因此片段保持源画面轨道。
    片段保存为 revision=1 的全新场景，返回其完整快照。

    - 源场景不存在：404；
    - from_ms >= to_ms、时间为布尔/字符串、target_scene_id 为空等
      非法请求：422（Pydantic 校验）；窗口内无相交字幕：422；
    - expected_revision 过期或超前、target_scene_id 已被占用（含与源同名）：409。

    版本核对分两道：读取源快照时先核对 record.revision ==
    expected_revision——裁剪所用的 track_count、字幕与源分轨全部取自
    这份快照，读取时版本不符立即 409，绝不拿错配快照裁剪；提交前在
    BEGIN IMMEDIATE 事务内再核对一次，覆盖“读取匹配后、提交前源被
    修订”的窗口。任一道失败既不会留下目标场景，也不会改动源场景或
    任何播放会话（播放会话本就是不被场景读取所修改的内存快照）。
    目标创建后可直接用既有读取、修订与播放接口操作。
    """
    _check_scene_id(scene_id)

    record = get_store().get_scene(scene_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"场景 {scene_id!r} 不存在"
        )
    # 读取即核对：track_count、字幕与分轨都来自 record，它的版本必须与
    # expected_revision 完全一致（过期或超前都算冲突）。current_revision
    # 如实报告“本次读到的版本”，客户端可据此重新 GET 后重试。
    if record.revision != req.expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": scene_id,
                "current_revision": record.revision,
            },
        )

    try:
        subs, tracks, changes = _build_clip(
            record, req.from_ms, req.to_ms
        )
    except EmptyClip:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "裁剪窗口 [from_ms, to_ms) 内没有与任何字幕相交的时段",
                "scene_id": scene_id,
                "from_ms": req.from_ms,
                "to_ms": req.to_ms,
            },
        )

    subtitles_data = [
        {
            "id": s.id,
            "speaker": s.speaker,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "locked_track": s.locked_track,
        }
        for s in subs
    ]
    assignments_data = [
        {"id": s.id, "track": t} for s, t in zip(subs, tracks)
    ]

    try:
        target_record = get_store().commit_clip(
            source_scene_id=scene_id,
            target_scene_id=req.target_scene_id,
            expected_revision=req.expected_revision,
            track_count=record.track_count,
            subtitles=subtitles_data,
            assignments=assignments_data,
            changes=changes,
        )
    except SourceSceneMissing:
        # 源在读取后、事务内被删除（正常流程中场景不可删除）：按 404 处理
        raise HTTPException(
            status_code=404, detail=f"场景 {scene_id!r} 不存在"
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "场景版本冲突：expected_revision 与当前版本不一致",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )
    except TargetSceneExists as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "target_scene_id 已被占用，裁剪目标必须使用新的 scene_id",
                "scene_id": exc.scene_id,
                "current_revision": exc.current_revision,
            },
        )

    return _record_to_scene_response(target_record)


# ---------------------------------------------------------------------------
# 播放会话：创建 / 推进 / 接纳
# ---------------------------------------------------------------------------


@app.post("/sessions", response_model=SessionStateResponse)
def create_session(req: SessionCreateRequest) -> SessionStateResponse:
    """按 scene_id 创建播放会话。

    复制场景当前的完整快照（字幕、分轨、changes、源版本），游标从
    0 毫秒开始。会话为单进程内存态，不跨重启保留；场景不存在返回 404。
    """
    _check_scene_id(req.scene_id)
    record = get_store().get_scene(req.scene_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"场景 {req.scene_id!r} 不存在"
        )
    return SessionStateResponse(**get_playback().create(record))


def _get_session_or_404(session_id: str) -> PlaybackSession:
    session = get_playback().get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=f"播放会话 {session_id!r} 不存在"
        )
    return session


@app.post("/sessions/{session_id}/advance", response_model=AdvanceResponse)
def advance_session(session_id: str, req: AdvanceRequest) -> AdvanceResponse:
    """推进游标到 time_ms（非负整数，只能不减）。

    返回源版本、游标，以及游标时刻处于 [start_ms, end_ms) 区间的字幕
    及其轨道。倒退返回 422 且会话不变；会话不存在返回 404。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().advance(session, req.time_ms)
    except CursorRegression as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "time_ms 只能不减，不得小于当前游标",
                "cursor_ms": exc.cursor_ms,
                "time_ms": exc.time_ms,
            },
        )
    return AdvanceResponse(**state)


@app.post("/sessions/{session_id}/events", response_model=AdvanceEventsResponse)
def advance_session_events(
    session_id: str, req: AdvanceRequest
) -> AdvanceEventsResponse:
    """推进游标并返回本次跨过的全部字幕进入/退出事件。

    与 /advance 共用同一会话游标，同样只能不减；额外返回事件窗口
    （旧游标, time_ms] 内的全部事件（旧游标 < 事件时刻 <= time_ms）：
    字幕在 start_ms 进入、end_ms 退出，短字幕即使开始和结束都落在
    两次采样之间，也会在跨过它的这次推进中成对返回。0 毫秒已活动的
    字幕（start_ms 为 0）由创建会话响应的快照确定，不产生进入事件。

    事件按时刻升序；同一时刻先退出后进入；同类型按会话字幕顺序。
    响应还含推进后的活动字幕（[start_ms, end_ms)）、源版本与游标。
    等时推进（time_ms 等于当前游标）事件为空。事件生成与游标更新
    在会话锁内与推进、接纳完整串行；事件不保存历史、不提供重放。

    倒退或非法时间返回 422 且会话不变；会话不存在返回 404。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().advance_events(session, req.time_ms)
    except CursorRegression as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "time_ms 只能不减，不得小于当前游标",
                "cursor_ms": exc.cursor_ms,
                "time_ms": exc.time_ms,
            },
        )
    return AdvanceEventsResponse(**state)


@app.post("/sessions/{session_id}/accept", response_model=SessionStateResponse)
def accept_scene_revision(session_id: str) -> SessionStateResponse:
    """接纳场景当前快照，让播放中的会话跟上后续修订。

    场景读取、已开始字幕校验、分轨重算与会话替换在同一会话锁内作为
    完整操作串行生效：并发接纳时，每个请求接纳的都是轮到它生效时的
    场景快照，迟到的请求不会把会话回写到旧版本。

    - 轨道数不得变；
    - 会话中 start_ms <= 游标的字幕，其 id、speaker、起止时间、
      locked_track 必须不变，并固定原分轨；
    - 其余字幕可改、可删、可新增，但都必须在游标之后开始；
    - 按新快照顺序与最少换轨、并列规则重算全部分轨。

    冲突或无解返回 409 且会话不变；成功则整体替换会话快照与源版本
    （场景本身不变），返回会话最新完整状态。
    """
    session = _get_session_or_404(session_id)
    try:
        state = get_playback().accept(
            session, lambda: get_store().get_scene(session.scene_id)
        )
    except SceneUnavailable:
        raise HTTPException(
            status_code=404, detail=f"场景 {session.scene_id!r} 不存在"
        )
    except AcceptConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "scene_id": session.scene_id},
        )
    return SessionStateResponse(**state)
