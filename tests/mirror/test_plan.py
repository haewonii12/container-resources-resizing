from datetime import datetime, timedelta, timezone

import pytest

from rightsizing.config import MirrorConfig
from rightsizing.mirror.clean import content_hash
from rightsizing.mirror.plan import AppInfo, Inputs, MirrorState, build_plan
from rightsizing.mirror.scan import RepoScan

T0 = datetime(2026, 11, 25, 2, 30, tzinfo=timezone.utc)
CFG = MirrorConfig()


def synced(name, managed=True):
    return AppInfo(name, managed=managed, synced_ever=True, operation_pending=False)


def inputs(cluster, repo_apps=(), apps=None, state=None, now=T0, adoptable=None, files=None, mirror_commit=None,
           foreign=None, ns=("development",)):
    repo = RepoScan(apps={("development", n): f"development/{n}" for n in repo_apps}, foreign=foreign or {})
    if apps is None:
        apps = {f"development/{n}": synced(n) for n in repo_apps}
    return Inputs(
        namespaces=list(ns), cluster_names={"development": cluster if cluster is None else set(cluster)},
        adoptable={"development": adoptable if adoptable is not None else {n: f"yaml-{n}" for n in (cluster or [])}},
        skipped={}, repo=repo, repo_files=files or {}, last_commit_is_mirror=mirror_commit or {},
        apps_by_path=apps, paths_on_disk={f"development/{n}" for n in repo_apps} | set(foreign or {}),
        state=state or MirrorState(), now=now)


def test_new_deployment_is_added_with_application():
    p = build_plan(inputs(["web", "new"], repo_apps=["web"]), CFG)
    assert p.add_dir == [("development", "new", "yaml-new")] and p.create_app == [("development", "new")]
    assert p.state.exports["development/new"] == content_hash("yaml-new") and p.exit_code == 0


def test_existing_app_untouched_and_rerun_idempotent():
    p = build_plan(inputs(["web"], repo_apps=["web"], files={"development/web": "human content"}), CFG)
    assert p.is_empty() and p.exit_code == 0
    p2 = build_plan(inputs(["web"], repo_apps=["web"], state=p.state), CFG)
    assert p2.is_empty() and p2.state.to_dict() == p.state.to_dict()


def test_foreign_dir_and_application_path_block_duplicate_add():
    p = build_plan(inputs(["kust", "byapp"], foreign={"development/kust": "foreign_layout:kustomize_or_helm"},
                          apps={"development/byapp": synced("byapp", managed=False)}), CFG)
    assert p.add_dir == []


def test_delete_requires_two_ok_runs_and_interval():
    s1 = build_plan(inputs([], repo_apps=["web"], now=T0, state=MirrorState(seen={"development/web"})), CFG)
    assert not s1.delete_dir and s1.state.missing["development/web"]["consecutive_ok_runs"] == 1
    s2 = build_plan(inputs([], repo_apps=["web"], now=T0 + timedelta(minutes=5), state=s1.state), CFG)
    assert not s2.delete_dir  # 2 runs but < 12h (e.g. init pod right after the cron run)
    s3 = build_plan(inputs([], repo_apps=["web"], now=T0 + timedelta(hours=13), state=s2.state), CFG)
    assert s3.delete_dir == [] and s3.halts == {"development": "mass_delete:empty_namespace"}


def test_delete_after_confirmation_in_non_empty_namespace():
    st = MirrorState(seen={"development/web", "development/api"})
    s1 = build_plan(inputs(["api"], repo_apps=["web", "api"], now=T0, state=st), CFG)
    s2 = build_plan(inputs(["api"], repo_apps=["web", "api"], now=T0 + timedelta(hours=24), state=s1.state), CFG)
    assert s2.delete_dir == [("development", "web")] and s2.delete_app == ["development/web"]
    assert "development/web" not in s2.state.missing and "development/web" not in s2.state.seen
    assert s2.exit_code == 0


def test_list_error_resets_counters_and_blocks_deletes():
    st = MirrorState(seen={"development/web", "development/api"})
    s1 = build_plan(inputs(["api"], repo_apps=["web", "api"], now=T0, state=st), CFG)
    err = build_plan(inputs(None, repo_apps=["web", "api"], now=T0 + timedelta(hours=24), state=s1.state), CFG)
    assert err.list_errors == ["development"] and err.exit_code == 2 and not err.delete_dir
    assert err.state.missing == {}
    s3 = build_plan(inputs(["api"], repo_apps=["web", "api"], now=T0 + timedelta(hours=48), state=err.state), CFG)
    assert not s3.delete_dir and s3.state.missing["development/web"]["consecutive_ok_runs"] == 1


def test_returning_deployment_clears_missing():
    st = MirrorState(seen={"development/web", "development/api"})
    s1 = build_plan(inputs(["api"], repo_apps=["web", "api"], state=st), CFG)
    s2 = build_plan(inputs(["api", "web"], repo_apps=["web", "api"], now=T0 + timedelta(hours=24), state=s1.state),
                    CFG)
    assert s2.state.missing == {} and not s2.delete_dir


@pytest.mark.parametrize("seen,info", [
    (set(), synced("web")),  # never observed live
    ({"development/web"}, AppInfo("web", True, synced_ever=False, operation_pending=False)),  # never synced
    ({"development/web"}, None),  # no Application at all
])
def test_never_deployed_is_never_deleted(seen, info):
    apps = {"development/api": synced("api")}
    if info:
        apps["development/web"] = info
    st = MirrorState(seen=seen | {"development/api"})
    s1 = build_plan(inputs(["api"], repo_apps=["web", "api"], apps=apps, state=st), CFG)
    s2 = build_plan(inputs(["api"], repo_apps=["web", "api"], apps=apps, now=T0 + timedelta(days=3), state=s1.state),
                    CFG)
    assert not s2.delete_dir and s2.skipped["development/web"] == "never_deployed"


def test_seen_marks_every_live_deployment():
    p = build_plan(inputs(["web", "helmy"], repo_apps=["web"], adoptable={"web": "x"}), CFG)
    assert {"development/web", "development/helmy"} <= p.state.seen


def _confirmed(names_live, repo_apps):
    st = MirrorState(seen={f"development/{n}" for n in repo_apps})
    s1 = build_plan(inputs(names_live, repo_apps=repo_apps, state=st), CFG)
    return build_plan(inputs(names_live, repo_apps=repo_apps, now=T0 + timedelta(hours=24), state=s1.state), CFG)


def test_mass_delete_count_guard():
    repo = [f"a{i}" for i in range(20)]
    p = _confirmed(repo[4:], repo)  # 4 deletes > max 3
    assert not p.delete_dir and p.halts["development"].startswith("mass_delete:count") and p.exit_code == 4


def test_mass_delete_ratio_guard_only_for_bigger_namespaces():
    repo = [f"a{i}" for i in range(5)]
    p = _confirmed(repo[2:], repo)  # 2/5 = 0.4 > 0.3
    assert not p.delete_dir and p.halts["development"].startswith("mass_delete:ratio")
    small = _confirmed(["b"], ["a", "b"])  # 1/2 in a 2-app namespace: ratio not applied
    assert small.delete_dir == [("development", "a")] and not small.halts


def test_selfheal_creates_missing_application_and_removes_orphan_application():
    apps = {"development/gone": synced("gone", managed=True), "development/humans": synced("humans", managed=False)}
    p = build_plan(inputs(["web"], repo_apps=["web"], apps=apps), CFG)
    assert ("development", "web") in p.create_app
    assert p.delete_app == ["development/gone"]  # managed + path absent on disk; human-made app left alone


def test_selfheal_never_deletes_application_of_foreign_dir():
    apps = {"development/kust": synced("kust", managed=True)}
    p = build_plan(inputs(["x"], apps=apps, foreign={"development/kust": "foreign_layout:kustomize_or_helm"},
                          adoptable={}), CFG)
    assert p.delete_app == []


def _never_synced():
    return AppInfo("new", managed=True, synced_ever=False, operation_pending=False)


def test_refresh_only_never_synced_mirror_apps_without_human_edits():
    exported = "old-export"
    st = MirrorState(exports={"development/new": content_hash(exported)})
    base = dict(repo_apps=["new"], adoptable={"new": "fresh-live"}, files={"development/new": exported},
                mirror_commit={"development/new": True}, state=st)
    p = build_plan(inputs(["new"], apps={"development/new": _never_synced()}, **base), CFG)
    assert p.refresh_dir == [("development", "new", "fresh-live")]
    assert p.state.exports["development/new"] == content_hash("fresh-live")
    # human edited the file (hash != last export) -> never overwritten
    edited = dict(base, files={"development/new": "human fix"})
    p2 = build_plan(inputs(["new"], apps={"development/new": _never_synced()}, **edited), CFG)
    assert not p2.refresh_dir and p2.skipped["development/new"] == "human_edited"
    # last commit by a human -> no refresh
    p3 = build_plan(inputs(["new"], apps={"development/new": _never_synced()},
                           **dict(base, mirror_commit={"development/new": False})), CFG)
    assert not p3.refresh_dir
    # synced once -> git owns the content
    p4 = build_plan(inputs(["new"], apps={"development/new": synced("new")}, **base), CFG)
    assert not p4.refresh_dir and "development/new" not in p4.skipped


def test_state_has_no_per_run_timestamps():
    st = MirrorState(seen={"development/web"})
    a = build_plan(inputs(["web"], repo_apps=["web"], state=st, now=T0), CFG).state.to_dict()
    b = build_plan(inputs(["web"], repo_apps=["web"], state=st, now=T0 + timedelta(days=1)), CFG).state.to_dict()
    assert a == b



def test_finalizer_app_is_kept_and_needs_a_human():
    apps = {"development/web": AppInfo("web", True, True, False, has_resources_finalizer=True),
            "development/api": synced("api")}
    st = MirrorState(seen={"development/web", "development/api"})
    s1 = build_plan(inputs(["api"], repo_apps=["web", "api"], apps=apps, state=st), CFG)
    s2 = build_plan(inputs(["api"], repo_apps=["web", "api"], apps=apps, now=T0 + timedelta(hours=24), state=s1.state),
                    CFG)
    assert not s2.delete_dir and s2.manual == {"development/web": "needs_manual_delete"} and s2.exit_code == 4
    assert s2.state.missing["development/web"]["consecutive_ok_runs"] == 2  # still counted, still alerting


def test_state_is_pruned_for_paths_gone_everywhere():
    st = MirrorState(seen={"development/old", "development/web"}, missing={"development/old": {
        "first_missing_at": T0.isoformat(), "consecutive_ok_runs": 1}}, exports={"development/old": "h"})
    p = build_plan(inputs(["web"], repo_apps=["web"], state=st), CFG)
    assert p.state.seen == {"development/web"} and p.state.missing == {} and p.state.exports == {}
