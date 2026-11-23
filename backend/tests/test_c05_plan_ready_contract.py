"""Check the C05 route-only revision example against the saved plan."""


def test_route_only_replan_preserves_requirements_in_plan_ready(example):
    previous = example("confirmed-plan.json")
    revised = example("revised-plan.json")
    event = example("plan-ready-revision-event.json")
    payload = event["payload"]
    plan_id = revised["planId"]

    assert payload["planId"] == previous["planId"] == plan_id
    assert payload["planVersion"] == revised["version"] == previous["version"] + 1
    assert revised["routes"] != previous["routes"]
    assert (
        payload["requirements"] == revised["resourceNeeds"] == previous["resourceNeeds"]
    )
    assert payload["decisionId"] == revised["decisionId"]
    assert event["affected"]["planIds"] == [plan_id]
    assert event["entityVersions"][plan_id] == revised["version"]
    assert event["scope"] == revised["runScope"]["scope"] == "simulation"
    assert event["runId"] == revised["runScope"]["runId"]
