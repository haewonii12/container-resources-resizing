"""Config switches, operation_pending, and the adopt / first-sync plan decisions (US-002, US-004)."""

import pytest
from pydantic import ValidationError

from rightsizing.config import Config, MirrorConfig
from rightsizing.mirror.clean import content_hash, to_yaml
from rightsizing.mirror.plan import AppInfo, MirrorState, build_plan
from rightsizing.mirror.run import index_applications
from tests.mirror.test_plan import inputs

ADOPT = MirrorConfig(adopt_drift_namespaces=["development"])
AUTO = MirrorConfig(auto_first_sync_namespaces=["development"])


def doc(replicas=1, image="nginx:1.27"):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "development"},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": "app", "image": image}]}}}}


def app(synced=True, managed=True, pending=False, **kw):
    return AppInfo("web", managed=managed, synced_ever=synced, operation_pending=pending, **kw)


# ------------------------------------------------------------------ US-002 config + operation_pending

def test_config_accepts_both_lists_within_mirror_namespaces():
    cfg = Config.model_validate({"mirror": {"auto_first_sync_namespaces": ["development"],
                                            "adopt_drift_namespaces": ["inference", "development"]}})
    assert cfg.mirror.auto_first_sync_namespaces == ["development"]
    assert MirrorConfig().auto_first_sync_namespaces == [] and MirrorConfig().adopt_drift_namespaces == []


@pytest.mark.parametrize("field", ["auto_first_sync_namespaces", "adopt_drift_namespaces"])
def test_config_rejects_namespace_outside_mirror_namespaces(field):
    with pytest.raises(ValidationError, match=f"mirror.{field} must be a subset of mirror.namespaces"):
        Config.model_validate({"mirror": {"namespaces": ["development"], field: ["inference"]}})
    with pytest.raises(ValidationError, match=field):  # checked after mirror.namespaces defaults to namespaces
        Config.model_validate({"mirror": {field: ["staging"]}})


@pytest.mark.parametrize("app_fields,pending", [
    ({"status": {"operationState": {"phase": "Succeeded"}, "history": [{"id": 1}]}}, False),
    ({"status": {"operationState": {"phase": "Failed"}}}, False),
    ({"status": {"operationState": {"phase": "Running"}}}, True),
    ({"status": {"operationState": {"phase": "Terminating"}}}, True),
    ({"operation": {"sync": {"revision": "a" * 40}}, "status": {}}, True),
    ({"status": {}}, False),
])
def test_operation_pending_signal(app_fields, pending):
    cfg = Config.model_validate({"git": {"url": "https://g/x.git"}})
    a = {"metadata": {"name": "web"}, "spec": {"source": {"repoURL": "https://g/x.git", "path": "development/web"}},
         **app_fields}
    assert index_applications(cfg, [a])["development/web"][1].operation_pending is pending


# ------------------------------------------------------------------ US-004 plan

def adopt_inputs(live, repo_doc, info=None, unsynced=(), skipped=None, **kw):
    inp = inputs(["web"], repo_apps=["web"], apps={"development/web": info or app()}, **kw)
    inp.live_docs = {"development/web": live} if live is not None else {}
    inp.repo_docs = {"development/web": repo_doc}
    inp.unsynced_commits = {"development/web": list(unsynced)}
    inp.skipped.update(skipped or {})
    return inp


def test_adopt_synced_app_with_drift_records_changes_and_overwrites():
    over = [("a" * 40, "rightsizing"), ("b" * 40, "human")]
    p = build_plan(adopt_inputs(doc(replicas=3), doc(), unsynced=over), ADOPT)
    assert p.adopt_dir == [("development", "web", to_yaml(doc(replicas=3)), ["replicas 1→3"], over)]
    assert p.state.exports["development/web"] == content_hash(to_yaml(doc(replicas=3)))


@pytest.mark.parametrize("info,live,reason", [
    (app(), doc(), None),  # no drift
    (app(pending=True), doc(replicas=3), "operation_pending"),
    (app(managed=False), doc(replicas=3), "not_managed"),
    (app(synced=False), doc(replicas=3), None),  # never synced: refresh / first sync own it
])
def test_adopt_only_for_synced_managed_idle_apps(info, live, reason):
    p = build_plan(adopt_inputs(live, doc(), info=info), ADOPT)
    assert p.adopt_dir == [] and p.sync_skipped.get("development/web") == reason


def test_foreign_tracked_is_never_adopted():
    p = build_plan(adopt_inputs(None, doc(), skipped={"development/web": "foreign_tracked"}), ADOPT)
    assert p.adopt_dir == [] and p.sync_skipped == {"development/web": "foreign_tracked"}


def test_adopt_only_in_listed_namespaces():
    p = build_plan(adopt_inputs(doc(replicas=3), doc()), MirrorConfig())
    assert p.adopt_dir == [] and p.sync_skipped == {}


def _refresh_inputs(human=True):
    st = MirrorState(exports={"development/new": content_hash("old-export")})
    inp = inputs(["new"], repo_apps=["new"], adoptable={"new": "fresh-live"},
                 files={"development/new": "human fix" if human else "old-export"},
                 mirror_commit={"development/new": not human}, state=st,
                 apps={"development/new": AppInfo("new", True, synced_ever=False, operation_pending=False)})
    inp.unsynced_commits = {"development/new": [("c" * 40, "human")]}
    return inp


def test_refresh_bypasses_human_edited_in_adopt_namespace():
    assert build_plan(_refresh_inputs(), MirrorConfig()).skipped["development/new"] == "human_edited"
    p = build_plan(_refresh_inputs(), ADOPT)
    assert p.refresh_dir == [("development", "new", "fresh-live")] and "development/new" not in p.skipped
    assert p.refresh_overwrote == {"development/new": [("c" * 40, "human")]}


def test_first_sync_create_for_apps_added_this_run():
    inp = inputs(["web", "new"], repo_apps=["web"], apps={"development/web": app()})
    p = build_plan(inp, AUTO)
    assert p.add_dir[0][:2] == ("development", "new") and p.first_sync == [("development", "new", "create")]


def test_first_sync_recreate_for_refreshed_this_run():
    inp = _refresh_inputs(human=False)
    inp.live_docs = {"development/new": doc()}
    p = build_plan(inp, AUTO)
    assert p.refresh_dir and p.first_sync == [("development", "new", "recreate")]


def never(**kw):
    return AppInfo("web", True, synced_ever=False, operation_pending=False, **kw)


@pytest.mark.parametrize("info,mirror_last,repo_doc,skipped,reason", [
    (never(), True, doc(), None, None),
    (never(), True, doc(replicas=5), None, "live_differs"),
    (never(), False, doc(), None, "human_last_commit"),
    (AppInfo("web", True, False, operation_pending=True), True, doc(), None, "operation_pending"),
    (never(has_finalizers=True), True, doc(), None, "finalizer"),
    (AppInfo("web", False, False, False), True, doc(), None, "not_managed"),
    (never(), True, doc(), "foreign_tracked", "foreign_tracked"),
])
def test_first_sync_recreate_guards(info, mirror_last, repo_doc, skipped, reason):
    inp = inputs(["web"], repo_apps=["web"], apps={"development/web": info}, adoptable={},
                 files={"development/web": "same"}, mirror_commit={"development/web": mirror_last})
    inp.live_docs, inp.repo_docs = {"development/web": doc()}, {"development/web": repo_doc}
    if skipped:
        inp.skipped["development/web"] = skipped
    p = build_plan(inp, AUTO)
    if reason is None:
        assert p.first_sync == [("development", "web", "recreate")] and p.sync_skipped == {}
    else:
        assert p.first_sync == [] and p.sync_skipped == {"development/web": reason}


def test_selfheal_create_never_gets_a_first_sync():
    inp = inputs(["web"], repo_apps=["web"], apps={})  # repo dir without an Application
    inp.live_docs, inp.repo_docs = {"development/web": doc()}, {"development/web": doc()}
    p = build_plan(inp, AUTO)
    assert ("development", "web") in p.create_app and p.first_sync == []


def test_empty_lists_give_the_same_plan_as_before():
    """With both lists empty, the new inputs change nothing (fixture plan == pre-change plan)."""
    def scenario(with_new_inputs):
        st = MirrorState(seen={"development/web", "development/gone"},
                         exports={"development/new": content_hash("old-export")})
        inp = inputs(["web", "new", "added"], repo_apps=["web", "new", "gone"], state=st,
                     adoptable={"new": "fresh-live", "added": "yaml-added"},
                     files={"development/new": "human fix", "development/web": "w"},
                     apps={"development/web": app(), "development/gone": app(),
                           "development/new": AppInfo("new", True, False, False)})
        if with_new_inputs:
            inp.live_docs = {"development/web": doc(replicas=9), "development/new": doc()}
            inp.repo_docs = {"development/web": doc(), "development/new": doc()}
            inp.unsynced_commits = {"development/new": [("c" * 40, "human")]}
        return build_plan(inp, MirrorConfig())
    assert scenario(True) == scenario(False)
    assert scenario(True).skipped["development/new"] == "human_edited"
