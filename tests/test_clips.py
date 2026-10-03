"""场景片段另存（裁剪）接口测试。

POST /scenes/{scene_id}/clip：把 [from_ms, to_ms) 另存为从 0 毫秒起、
保持源画面轨道的 revision=1 新场景。覆盖裁剪语义、保轨、边界、
404/409/422、原子性、源不变、既有接口可用，以及“读取的源快照必须与
expected_revision 一致”的可控交错回归。
"""
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.solver import Subtitle, solve  # noqa: E402


def make_source(
    client,
    scene_id="scene-src",
    subtitles=None,
    track_count=2,
    expected_revision=0,
):
    if subtitles is None:
        # s1[0,1000) Alice；s2[800,3000) Bob 锁 2；s3[1000,2500) Alice
        # 分轨 s1=1, s2=2, s3=1，changes=0
        subtitles = [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ]
    resp = client.put(
        f"/scenes/{scene_id}",
        json={
            "track_count": track_count,
            "expected_revision": expected_revision,
            "subtitles": subtitles,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def clip(client, scene_id, target, expected_revision, from_ms, to_ms):
    return client.post(
        f"/scenes/{scene_id}/clip",
        json={
            "target_scene_id": target,
            "expected_revision": expected_revision,
            "from_ms": from_ms,
            "to_ms": to_ms,
        },
    )


# ---------------------------------------------------------------------------
# 基本裁剪语义
# ---------------------------------------------------------------------------


def test_clip_basic_shifts_times_and_preserves_tracks():
    client = TestClient(app)
    src = make_source(client)

    # 窗口 [900, 3000)：s1 尾部 [900,1000)、s2 整段 [900,3000)、
    # s3 整段 [1000,2500) 相交
    resp = clip(client, "scene-src", "scene-clip", 1, 900, 3000)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["scene_id"] == "scene-clip"
    assert body["revision"] == 1
    assert body["track_count"] == 2
    assert body["feasible"] is True

    assert body["subtitles"] == [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 100,
         "locked_track": 1},
        {"id": "s2", "speaker": "Bob", "start_ms": 0, "end_ms": 2100,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 100, "end_ms": 1600,
         "locked_track": 1},
    ]
    # 分轨与源画面轨道完全一致
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]

    # 片段已作为独立场景持久化
    got = client.get("/scenes/scene-clip").json()
    assert got["revision"] == 1
    assert got["subtitles"] == body["subtitles"]
    assert got["assignments"] == src["assignments"]


def test_clip_keeps_source_order_ids_and_speakers():
    client = TestClient(app)
    subtitles = [
        {"id": 9, "speaker": "Zoe", "start_ms": 0, "end_ms": 100},
        {"id": "x", "speaker": "Abe", "start_ms": 50, "end_ms": 200},
        {"id": 1, "speaker": "Zoe", "start_ms": 200, "end_ms": 300},
    ]
    make_source(client, "scene-order", subtitles)
    resp = clip(client, "scene-order", "scene-order-out", 1, 0, 1000)
    assert resp.status_code == 200
    subs = resp.json()["subtitles"]
    assert [s["id"] for s in subs] == [9, "x", 1]
    assert [s["speaker"] for s in subs] == ["Zoe", "Abe", "Zoe"]


def test_clip_boundary_intersection_is_half_open():
    client = TestClient(app)
    # s1[100,200) s2[200,300) s3[300,400)
    make_source(
        client,
        "scene-bound",
        subtitles=[
            {"id": "s1", "speaker": "A", "start_ms": 100, "end_ms": 200},
            {"id": "s2", "speaker": "B", "start_ms": 200, "end_ms": 300},
            {"id": "s3", "speaker": "C", "start_ms": 300, "end_ms": 400},
        ],
    )

    # 窗口 [200,300)：s1 end==from 不相交（右开），s3 start==to 不相交（左闭）
    resp = clip(client, "scene-bound", "scene-bound-out", 1, 200, 300)
    assert resp.status_code == 200
    subs = resp.json()["subtitles"]
    assert [s["id"] for s in subs] == ["s2"]
    assert subs[0]["start_ms"] == 0
    assert subs[0]["end_ms"] == 100
    assert subs[0]["locked_track"] == 1  # 单条源分轨为 1


def test_clip_partial_overlap_at_window_edges():
    client = TestClient(app)
    make_source(
        client,
        "scene-edges",
        subtitles=[
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 500},
            {"id": "b", "speaker": "B", "start_ms": 400, "end_ms": 600},
            {"id": "c", "speaker": "C", "start_ms": 1500, "end_ms": 2000},
        ],
    )
    # 窗口 [400,1500)：a 裁成 [400,500)->[0,100)，b [400,600)->[0,200)，
    # c start==to 不相交
    resp = clip(client, "scene-edges", "scene-edges-out", 1, 400, 1500)
    assert resp.status_code == 200
    subs = resp.json()["subtitles"]
    assert [(s["id"], s["start_ms"], s["end_ms"]) for s in subs] == [
        ("a", 0, 100),
        ("b", 0, 200),
    ]


def test_clip_starts_at_zero_and_recomputes_changes():
    """片段时间轴从 0 毫秒起；changes 按片段字幕按既有规则重算。"""
    client = TestClient(app)
    # 同一说话人被锁定到不同轨：源 changes=1（a->1, b->2）
    subtitles = [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100,
         "locked_track": 1},
        {"id": "b", "speaker": "A", "start_ms": 100, "end_ms": 200,
         "locked_track": 2},
    ]
    src = make_source(client, "scene-chg", subtitles)
    assert src["changes"] == 1

    # 只保留 b：片段内单条字幕，换轨 0
    resp = clip(client, "scene-chg", "scene-chg-out", 1, 100, 200)
    assert resp.status_code == 200
    body = resp.json()
    assert body["subtitles"] == [
        {"id": "b", "speaker": "A", "start_ms": 0, "end_ms": 100,
         "locked_track": 2}
    ]
    assert body["changes"] == 0
    assert body["assignments"] == [{"id": "b", "track": 2}]


def test_clip_locked_tracks_equal_source_assignments_even_if_unlocked():
    """源里未显式锁轨的字幕，片段 locked_track 写入源求解出的轨道。"""
    client = TestClient(app)
    subtitles = [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100},
        {"id": "b", "speaker": "B", "start_ms": 50, "end_ms": 150},
        {"id": "c", "speaker": "A", "start_ms": 100, "end_ms": 200},
    ]
    src = make_source(client, "scene-lock", subtitles)
    assert src["assignments"] == [
        {"id": "a", "track": 1},
        {"id": "b", "track": 2},
        {"id": "c", "track": 1},
    ]
    body = clip(client, "scene-lock", "scene-lock-out", 1, 0, 200).json()
    assert [s["locked_track"] for s in body["subtitles"]] == [1, 2, 1]


# ---------------------------------------------------------------------------
# 422：窗口无相交字幕 / 非法请求
# ---------------------------------------------------------------------------


def test_clip_without_intersection_returns_422_and_leaves_nothing():
    client = TestClient(app)
    make_source(client)
    # 所有字幕都在 [5000,6000) 之前结束
    resp = clip(client, "scene-src", "scene-empty", 1, 5000, 6000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["from_ms"] == 5000
    assert detail["to_ms"] == 6000

    # 失败不留下目标，源不变
    assert client.get("/scenes/scene-empty").status_code == 404
    src = client.get("/scenes/scene-src").json()
    assert src["revision"] == 1
    assert len(src["subtitles"]) == 3


def test_clip_gap_between_subtitles_returns_422():
    client = TestClient(app)
    make_source(
        client,
        "scene-gap",
        subtitles=[
            {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 100},
            {"id": "b", "speaker": "B", "start_ms": 200, "end_ms": 300},
        ],
    )
    # [100,200) 两端都端点相接但左闭右开：无相交
    resp = clip(client, "scene-gap", "scene-gap-out", 1, 100, 200)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload_override",
    [
        {"from_ms": 100, "to_ms": 100},       # from == to
        {"from_ms": 200, "to_ms": 100},       # from > to
        {"from_ms": -1, "to_ms": 100},        # 负数
        {"from_ms": True, "to_ms": 100},      # 布尔值
        {"from_ms": 0, "to_ms": True},
        {"from_ms": False, "to_ms": 100},
        {"from_ms": "0", "to_ms": 100},       # 字符串
        {"from_ms": 0, "to_ms": "100"},
        {"from_ms": 1.0, "to_ms": 100},       # 浮点
        {"expected_revision": -1},            # 负版本号
        {"target_scene_id": "   "},           # 空白目标 id
        {"target_scene_id": ""},
    ],
)
def test_clip_invalid_request_returns_422(payload_override):
    client = TestClient(app)
    make_source(client)
    body = {
        "target_scene_id": "scene-bad",
        "expected_revision": 1,
        "from_ms": 0,
        "to_ms": 1000,
    }
    body.update(payload_override)
    resp = client.post("/scenes/scene-src/clip", json=body)
    assert resp.status_code == 422, resp.text
    assert client.get("/scenes/scene-bad").status_code == 404


def test_clip_missing_fields_422():
    client = TestClient(app)
    make_source(client)
    for body in (
        {"expected_revision": 1, "from_ms": 0, "to_ms": 100},
        {"target_scene_id": "t", "from_ms": 0, "to_ms": 100},
        {"target_scene_id": "t", "expected_revision": 1, "to_ms": 100},
        {"target_scene_id": "t", "expected_revision": 1, "from_ms": 0},
    ):
        resp = client.post("/scenes/scene-src/clip", json=body)
        assert resp.status_code == 422


def test_clip_blank_source_scene_id_422():
    client = TestClient(app)
    resp = client.post(
        "/scenes/%20%20/clip",
        json={"target_scene_id": "t", "expected_revision": 0,
              "from_ms": 0, "to_ms": 100},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 404 / 409
# ---------------------------------------------------------------------------


def test_clip_unknown_source_returns_404():
    client = TestClient(app)
    resp = clip(client, "no-such-scene", "scene-new", 0, 0, 100)
    assert resp.status_code == 404
    assert client.get("/scenes/scene-new").status_code == 404


def test_clip_stale_revision_returns_409():
    client = TestClient(app)
    make_source(client, "scene-rev")  # revision 1
    # 推进到 revision 2
    make_source(
        client,
        "scene-rev",
        expected_revision=1,
        subtitles=[
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 900},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000},
        ],
    )

    resp = clip(client, "scene-rev", "scene-rev-out", 1, 0, 1000)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "scene-rev"
    assert detail["current_revision"] == 2

    # 失败不留目标、源停留在 revision 2
    assert client.get("/scenes/scene-rev-out").status_code == 404
    assert client.get("/scenes/scene-rev").json()["revision"] == 2


def test_clip_expected_revision_zero_on_existing_source_409():
    client = TestClient(app)
    make_source(client)
    resp = clip(client, "scene-src", "scene-zero", 0, 0, 1000)
    assert resp.status_code == 409
    assert client.get("/scenes/scene-zero").status_code == 404


def test_clip_target_already_exists_returns_409():
    client = TestClient(app)
    make_source(client, "scene-a")
    make_source(client, "scene-b")  # 占用 scene-b

    resp = clip(client, "scene-a", "scene-b", 1, 0, 1000)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "scene-b"
    assert detail["current_revision"] == 1

    # 已存在目标保持原样（不会被片段覆盖）
    target = client.get("/scenes/scene-b").json()
    assert [s["id"] for s in target["subtitles"]] == ["s1", "s2", "s3"]
    # 源场景不受影响
    assert client.get("/scenes/scene-a").json()["revision"] == 1


def test_clip_target_same_as_source_returns_409():
    client = TestClient(app)
    make_source(client, "scene-same")
    resp = clip(client, "scene-same", "scene-same", 1, 0, 1000)
    assert resp.status_code == 409
    # 源没有被片段覆盖
    src = client.get("/scenes/scene-same").json()
    assert src["revision"] == 1
    assert len(src["subtitles"]) == 3


def test_clip_target_exists_conflict_check_after_empty_window_still_422():
    """窗口无相交（422 语义）优先于目标占用（409）：先裁得出片段才提交。"""
    client = TestClient(app)
    make_source(client, "scene-x")
    make_source(client, "scene-y")
    resp = clip(client, "scene-x", "scene-y", 1, 9000, 10000)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 原子性、源不变、独立性
# ---------------------------------------------------------------------------


def test_clip_does_not_modify_source_or_playback_sessions():
    client = TestClient(app)
    src_before = make_source(client, "scene-keep")
    sess = client.post(
        "/sessions", json={"scene_id": "scene-keep"}
    ).json()
    client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 900}
    )

    resp = clip(client, "scene-keep", "scene-keep-clip", 1, 900, 3000)
    assert resp.status_code == 200

    # 源场景快照逐字段不变
    assert client.get("/scenes/scene-keep").json() == src_before

    # 既有播放会话不被裁剪操作影响（游标仍在 900，快照仍为源）
    state = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 900}
    ).json()
    assert state["cursor_ms"] == 900
    assert state["source_revision"] == 1
    assert {a["id"] for a in client.get("/scenes/scene-keep").json()["assignments"]} == {
        "s1", "s2", "s3"
    }


def test_clip_is_independent_of_later_source_revision():
    client = TestClient(app)
    make_source(client, "scene-ind")
    resp = clip(client, "scene-ind", "scene-ind-out", 1, 0, 3000)
    assert resp.status_code == 200

    # 源继续修订到 revision 2；片段仍是 revision 1、内容不变
    make_source(
        client,
        "scene-ind",
        expected_revision=1,
        subtitles=[
            {"id": "only", "speaker": "Solo", "start_ms": 0, "end_ms": 50}
        ],
    )
    clip_got = client.get("/scenes/scene-ind-out").json()
    assert clip_got["revision"] == 1
    assert [s["id"] for s in clip_got["subtitles"]] == ["s1", "s2", "s3"]
    assert client.get("/scenes/scene-ind").json()["revision"] == 2


def test_clip_target_supports_existing_revise_read_and_playback():
    """片段目标沿用既有读取/修订/播放调用，旧接口不变。"""
    client = TestClient(app)
    make_source(client, "scene-play-src")
    resp = clip(client, "scene-play-src", "scene-play-clip", 1, 900, 3000)
    assert resp.status_code == 200

    # 修订片段：expected_revision=1，成功后 revision=2
    rev = client.put(
        "/scenes/scene-play-clip",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "s1", "speaker": "Alice", "start_ms": 0,
                 "end_ms": 100, "locked_track": 1},
                {"id": "s2", "speaker": "Bob", "start_ms": 0, "end_ms": 2100},
            ],
        },
    )
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2

    # 播放片段
    sess = client.post(
        "/sessions", json={"scene_id": "scene-play-clip"}
    ).json()
    assert sess["source_revision"] == 2
    assert sess["cursor_ms"] == 0
    adv = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 50}
    ).json()
    assert {a["id"] for a in adv["active"]} == {"s1", "s2"}

    # 事件推进同样可用（s1 在 100 退出）
    ev = client.post(
        f"/sessions/{sess['session_id']}/events", json={"time_ms": 100}
    ).json()
    assert ev["events"] == [
        {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 100,
         "type": "exit"}
    ]


def test_concurrent_clip_and_revision_never_mixes_snapshots():
    """并发裁剪与源修订：二者被数据库写锁串行，不出现混用/脏写。

    若裁剪先提交：片段 revision=1 基于 revision=1 的源，随后修订成功到
    revision=2；若修订先提交：裁剪收到 409。无论如何片段内容必须与某次
    完整提交一致，目标 revision 恒为 1。
    """
    client = TestClient(app)
    make_source(client, "scene-race")

    results = []
    barrier = threading.Barrier(2)

    def do_clip():
        local = TestClient(app)
        barrier.wait()
        resp = clip(local, "scene-race", "scene-race-clip", 1, 0, 3000)
        results.append(("clip", resp.status_code, resp.json()))

    def do_revise():
        local = TestClient(app)
        body = {
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": [
                {"id": "only", "speaker": "Solo",
                 "start_ms": 0, "end_ms": 50}
            ],
        }
        barrier.wait()
        resp = local.put("/scenes/scene-race", json=body)
        results.append(("revise", resp.status_code, resp.json()))

    threads = [threading.Thread(target=do_clip), threading.Thread(target=do_revise)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    clip_result = next(r for r in results if r[0] == "clip")
    revise_result = next(r for r in results if r[0] == "revise")
    assert revise_result[1] == 200
    assert revise_result[2]["revision"] == 2

    if clip_result[1] == 200:
        # 裁剪先生效：片段含 revision 1 的三条字幕
        body = clip_result[2]
        assert body["revision"] == 1
        assert [s["id"] for s in body["subtitles"]] == ["s1", "s2", "s3"]
    else:
        # 修订先生效：expected_revision=1 已过期
        assert clip_result[1] == 409
        assert clip_result[2]["detail"]["current_revision"] == 2


def test_concurrent_clips_to_same_target_only_one_succeeds():
    client = TestClient(app)
    make_source(client, "scene-multi")
    results = []
    barrier = threading.Barrier(4)

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = clip(local, "scene-multi", "scene-multi-out", 1, 0, 3000)
        results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 3
    assert client.get("/scenes/scene-multi-out").json()["revision"] == 1
    assert client.get("/scenes/scene-multi").json()["revision"] == 1


# ---------------------------------------------------------------------------
# 与求解器交叉验证
# ---------------------------------------------------------------------------


def test_clip_tracks_match_solver_with_locked_source_tracks():
    """片段 locked_track=源分轨时，求解器在源 track_count 下复现源轨道。"""
    client = TestClient(app)
    subtitles = [
        {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000,
         "locked_track": 1},
        {"id": "b", "speaker": "B", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "c", "speaker": "A", "start_ms": 1000, "end_ms": 2500},
        {"id": "d", "speaker": "C", "start_ms": 2000, "end_ms": 2600},
    ]
    src = make_source(client, "scene-solver", subtitles, track_count=3)
    src_tracks = {a["id"]: a["track"] for a in src["assignments"]}

    resp = clip(client, "scene-solver", "scene-solver-out", 1, 900, 2700)
    assert resp.status_code == 200
    body = resp.json()

    subs = [
        Subtitle(
            id=s["id"], speaker=s["speaker"], start_ms=s["start_ms"],
            end_ms=s["end_ms"], locked_track=s["locked_track"],
        )
        for s in body["subtitles"]
    ]
    tracks, changes = solve(subs, 3)
    assert [a["track"] for a in body["assignments"]] == tracks
    assert body["changes"] == changes
    # 每条片段字幕的轨道都与源画面轨道相同
    for s in body["subtitles"]:
        assert s["locked_track"] == src_tracks[s["id"]]


# ---------------------------------------------------------------------------
# 可控交错回归：裁剪使用的源快照必须与 expected_revision 一致
#
# 旧实现先在事务外 get_scene 读出源快照并据此构造片段，到 commit_clip
# 事务内才核对版本；于是 expected_revision=2 的请求在源仍为版本 1 时
# 读出版本 1，源随后升级到版本 2，提交时 2==2 核对通过，版本 1 的字幕
# 与分轨被当成“版本 2 的片段”保存——快照与核对版本错配。
#
# 修复后：源完整快照读取、版本核对、片段构造与目标写入在同一个
# BEGIN IMMEDIATE 事务内完成。因此：
# - 请求生效时版本不符 -> 读取即 409，绝不落库旧快照内容；
# - 若在锁内读到匹配快照，则该锁持有到提交，源版本期间不可能变化，
#   成功保存的片段必然逐字段来自该匹配快照（正常裁剪仍为版本 1）。
# ---------------------------------------------------------------------------


def revision2_subtitles():
    """版本 2 的源：单条 only[0,50)，与版本 1 的三条字幕完全不同。"""
    return [
        {"id": "only", "speaker": "Solo", "start_ms": 0, "end_ms": 50}
    ]


def upgrade_source_to_revision2(client, scene_id):
    return client.put(
        f"/scenes/{scene_id}",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": revision2_subtitles(),
        },
    )


def gate_clip_commit(monkeypatch):
    """故障注入：受控暂停裁剪提交事务执行 BEGIN IMMEDIATE 之前的时机。

    返回 (paused, release, arm)。arm() 之后，进程内第一个执行
    BEGIN IMMEDIATE 的连接（即裁剪请求的提交事务）在真正获取写锁之前
    被阻塞在 paused/release 上；暂停期间源修订仍可正常提交（此时裁剪
    尚未持有写锁）。注入一次性生效，之后所有连接恢复直通。

    这个切点恰好区分新旧实现：
    - 旧实现：此前已在事务外用 get_scene 读源快照（无 BEGIN，不被拦），
      片段也按该快照构造完成；暂停期间源升级后，事务内版本复核会把
      旧快照当作匹配版本写入——快照与核对版本错配；
    - 新实现：源快照读取就在这个 BEGIN IMMEDIATE 事务内，放行后读到的
      一定是当时的源版本，片段内容与核对版本必然一致。
    """
    from app import storage as storage_module

    paused = threading.Event()
    release = threading.Event()
    state = {"gated": False}

    real_connect = storage_module.sqlite3.connect

    class GatedConnection(storage_module.sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if (
                not state["gated"]
                and isinstance(sql, str)
                and sql.strip().upper().startswith("BEGIN IMMEDIATE")
            ):
                state["gated"] = True
                paused.set()
                assert release.wait(timeout=10), "测试未能及时放行裁剪事务"
            return super().execute(sql, parameters)

    def fake_connect(db_path, *args, **kwargs):
        kwargs.setdefault("factory", GatedConnection)
        return real_connect(db_path, *args, **kwargs)

    def arm():
        monkeypatch.setattr(storage_module.sqlite3, "connect", fake_connect)

    return paused, release, arm


def post_clip_in_thread(scene_id, target, expected_revision, responses, key):
    local = TestClient(app)
    responses[key] = clip(
        local, scene_id, target, expected_revision, 0, 3000
    )


def test_clip_expected_revision_2_reads_revision1_then_source_upgrades(
    monkeypatch,
):
    """expected_revision=2 的裁剪在源为版本 1 时读到版本 1，随后源升级
    到版本 2，裁剪事务才开始。放行后不得保存版本 1 的字幕与分轨。

    修复后源读取与版本核对都在裁剪的 BEGIN IMMEDIATE 事务内：放行时
    源已是版本 2，与 expected_revision 一致 -> 成功，且片段逐字段来自
    版本 2 快照。旧实现先在事务外读出版本 1 并据此构造片段，提交时
    2==2 复核通过，会成功保存版本 1 的三条字幕（本测试在旧代码下失败）。
    """
    client = TestClient(app)
    make_source(client, "scene-mismatch")  # revision 1
    paused, release, arm = gate_clip_commit(monkeypatch)

    # 建一个源会话，验证失败/成功都不影响播放会话
    sess = client.post(
        "/sessions", json={"scene_id": "scene-mismatch"}
    ).json()
    client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 900}
    )

    responses = {}
    arm()
    t = threading.Thread(
        target=post_clip_in_thread,
        args=("scene-mismatch", "scene-mismatch-out", 2, responses, "clip"),
    )
    t.start()
    assert paused.wait(timeout=10), "裁剪事务未到达受控的 BEGIN 前切点"

    # 裁剪挂起期间源从版本 1 升级到版本 2
    rev = upgrade_source_to_revision2(client, "scene-mismatch")
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2

    release.set()
    t.join(timeout=10)
    assert not t.is_alive()

    resp = responses["clip"]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 片段必须逐字段来自版本 2 快照，绝不能是挂起前读到的版本 1 三条
    assert body["revision"] == 1
    assert body["track_count"] == 2
    assert [s["id"] for s in body["subtitles"]] == ["only"]
    assert body["subtitles"] == [
        {"id": "only", "speaker": "Solo",
         "start_ms": 0, "end_ms": 50, "locked_track": 1}
    ]
    assert body["assignments"] == [{"id": "only", "track": 1}]
    persisted = client.get("/scenes/scene-mismatch-out").json()
    assert [s["id"] for s in persisted["subtitles"]] == ["only"]

    # 源场景停留在版本 2，未被裁剪操作改动
    src = client.get("/scenes/scene-mismatch").json()
    assert src["revision"] == 2
    assert [s["id"] for s in src["subtitles"]] == ["only"]

    # 播放会话不受影响（仍为创建时的版本 1 快照、游标 900）
    state = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 900}
    ).json()
    assert state["source_revision"] == 1
    assert state["cursor_ms"] == 900
    assert {(a["id"], a["track"]) for a in state["active"]} == {
        ("s1", 1), ("s2", 2)
    }


def test_clip_read_matches_then_source_revised_before_commit_returns_409(
    monkeypatch,
):
    """读取匹配 expected_revision 后、提交前源版本变化：必须 409，
    且不创建目标、不改源、不改播放会话。

    源修订先提交（版本 1 -> 2），裁剪事务随后才拿到写锁并读取：
    修复后读取即在锁内、读到版本 2，与 expected_revision=1 不符 ->
    读取时 409；旧实现的事务内复核同样返回 409。本测试锁定该语义并
    验证失败零副作用。
    """
    client = TestClient(app)
    make_source(client, "scope-rev2")  # revision 1
    paused, release, arm = gate_clip_commit(monkeypatch)

    sess = client.post(
        "/sessions", json={"scene_id": "scope-rev2"}
    ).json()

    responses = {}
    arm()
    t = threading.Thread(
        target=post_clip_in_thread,
        args=("scope-rev2", "scope-rev2-out", 1, responses, "clip"),
    )
    t.start()
    assert paused.wait(timeout=10), "裁剪事务未到达受控的 BEGIN 前切点"

    # 裁剪持有写锁之前先把源升级到版本 2
    rev = upgrade_source_to_revision2(client, "scope-rev2")
    assert rev.status_code == 200

    release.set()
    t.join(timeout=10)
    assert not t.is_alive()

    resp = responses["clip"]
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["scene_id"] == "scope-rev2"
    assert detail["current_revision"] == 2

    # 失败零副作用：无目标、源停留在版本 2
    assert client.get("/scenes/scope-rev2-out").status_code == 404
    src = client.get("/scenes/scope-rev2").json()
    assert src["revision"] == 2
    assert [s["id"] for s in src["subtitles"]] == ["only"]

    # 播放会话仍是版本 1 快照，不受失败裁剪与源修订影响
    state = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 900}
    ).json()
    assert state["source_revision"] == 1
    assert state["cursor_ms"] == 900
    assert {(a["id"], a["track"]) for a in state["active"]} == {
        ("s1", 1), ("s2", 2)
    }


def test_clip_normal_interleave_saves_matching_snapshot_as_revision1(
    monkeypatch,
):
    """正常裁剪的可控交错：源读取期间源未变化，放行后成功保存——
    片段时间归零、保持源画面轨道、保存为版本 1，目标可读取、修订、播放。
    """
    client = TestClient(app)
    make_source(client, "scope-ok")  # revision 1
    paused, release, arm = gate_clip_commit(monkeypatch)

    responses = {}
    arm()
    t = threading.Thread(
        target=post_clip_in_thread,
        args=("scope-ok", "scope-ok-out", 1, responses, "clip"),
    )
    t.start()
    assert paused.wait(timeout=10)
    # 挂起期间源保持版本 1，不做任何修订
    assert client.get("/scenes/scope-ok").json()["revision"] == 1
    release.set()
    t.join(timeout=10)
    assert not t.is_alive()

    resp = responses["clip"]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scene_id"] == "scope-ok-out"
    assert body["revision"] == 1
    assert body["track_count"] == 2
    # 窗口 [0,3000) 含全部三条，时间按源顺序、归零，分轨与源一致
    assert [(s["id"], s["start_ms"], s["end_ms"], s["locked_track"])
            for s in body["subtitles"]] == [
        ("s1", 0, 1000, 1),
        ("s2", 800, 3000, 2),
        ("s3", 1000, 2500, 1),
    ]
    assert body["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]

    # 目标可读取、修订（revision 1 -> 2）、播放
    got = client.get("/scenes/scope-ok-out").json()
    assert got == body
    rev = client.put(
        "/scenes/scope-ok-out",
        json={
            "track_count": 2,
            "expected_revision": 1,
            "subtitles": revision2_subtitles(),
        },
    )
    assert rev.status_code == 200
    assert rev.json()["revision"] == 2
    sess = client.post(
        "/sessions", json={"scene_id": "scope-ok-out"}
    ).json()
    assert sess["source_revision"] == 2
    adv = client.post(
        f"/sessions/{sess['session_id']}/advance", json={"time_ms": 10}
    ).json()
    assert [a["id"] for a in adv["active"]] == ["only"]

    # 源场景保持版本 1、内容不变
    src = client.get("/scenes/scope-ok").json()
    assert src["revision"] == 1
    assert [s["id"] for s in src["subtitles"]] == ["s1", "s2", "s3"]
