def test_request_budget_caps_each_timeout_and_spaces_every_request():
    from app.services.source_request_budget import RequestBudgetController

    clock = [0.0]
    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    controller = RequestBudgetController(
        total_seconds=5,
        request_timeout_seconds=12,
        interval_seconds=1,
        clock=lambda: clock[0],
        sleeper=sleeper,
    )

    assert controller.before_request() == 5
    clock[0] = 0.25
    assert controller.before_request() == 4
    assert sleeps == [0.75]
    clock[0] = 4.8
    assert 0 < controller.before_request() < 0.21
