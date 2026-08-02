from dataclasses import replace

import pytest

from api.health import HealthTracker
from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    ContentOrigin,
    ErrorCategory,
    PrivacyLevel,
    Role,
)
from api.routing import (
    Connectivity,
    NoRouteError,
    ProviderRegistry,
    ProviderTarget,
    RoutePlanner,
    RoutingPolicy,
)


def make_request(**changes):
    request = ChatRequest(
        model="logical",
        messages=(ChatMessage(Role.USER, "explain this", ContentOrigin.RAW_TRANSCRIPT),),
        mode="talk",
        language="en",
        privacy=PrivacyLevel.REMOTE_ALLOWED,
        max_output_tokens=32,
        remote_authorized=True,
    )
    return replace(request, **changes)


def targets():
    return (
        ProviderTarget(
            "local",
            "ollama",
            "small",
            False,
            languages=frozenset({"en", "it"}),
            context_window=512,
            max_output_tokens=64,
            priority=20,
        ),
        ProviderTarget(
            "remote",
            "groq",
            "large",
            True,
            languages=frozenset({"en"}),
            context_window=4096,
            max_output_tokens=256,
            priority=10,
        ),
    )


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (RoutingPolicy.LOCAL_ONLY, ["local"]),
        (RoutingPolicy.REMOTE_ONLY, ["remote"]),
        (RoutingPolicy.LOCAL_FIRST, ["local", "remote"]),
        (RoutingPolicy.REMOTE_FIRST, ["remote", "local"]),
    ],
)
def test_policy_order_is_deterministic(policy, expected):
    planner = RoutePlanner(targets(), policy=policy)
    result = planner.plan(make_request(), connectivity=Connectivity.ONLINE)
    assert [target.name for target in result] == expected


def test_auto_prefers_local_for_simple_request_and_remote_for_complex_think():
    planner = RoutePlanner(targets(), policy=RoutingPolicy.AUTO, auto_complexity_threshold=2)

    simple = planner.plan(make_request(), connectivity=Connectivity.ONLINE)
    complex_request = make_request(mode="think", options={"complex": True})
    complex_result = planner.plan(complex_request, connectivity=Connectivity.ONLINE)

    assert simple[0].name == "local"
    assert complex_result[0].name == "remote"
    assert planner.plan(complex_request, connectivity=Connectivity.UNKNOWN)[0].name == "local"


def test_mode_chains_and_unknown_connectivity_override_are_explicit():
    planner = RoutePlanner(
        targets(),
        policy="auto",
        auto_complexity_threshold=0,
        mode_candidates={"talk": ("remote", "local"), "think": ("local",)},
        allow_remote_when_connectivity_unknown=True,
    )

    assert planner.select(make_request(), connectivity="unknown").name == "remote"
    assert (
        planner.select(
            make_request(mode="think"),
            connectivity="online",
            complexity_threshold=0,
        ).name
        == "local"
    )


def test_eligibility_enforces_authorization_language_context_lists_and_health():
    request = make_request(remote_authorized=False)
    planner = RoutePlanner(targets(), policy="remote_only")
    with pytest.raises(NoRouteError):
        planner.plan(request, connectivity="online")

    planner = RoutePlanner(
        targets(),
        policy="local_first",
        allowlist={"ollama", "groq"},
        denylist={"groq"},
    )
    assert [target.name for target in planner.plan(make_request(), connectivity="online")] == [
        "local"
    ]

    with pytest.raises(NoRouteError):
        RoutePlanner(targets(), policy="remote_only").plan(
            make_request(language="it"), connectivity="online"
        )
    with pytest.raises(NoRouteError):
        RoutePlanner(targets(), policy="local_only").plan(
            make_request(max_output_tokens=500),
            connectivity="online",
            estimated_input_tokens=500,
        )

    health = HealthTracker(failures_to_open=1)
    health.record_failure("ollama/small", ErrorCategory.CONNECTIVITY)
    planner = RoutePlanner(targets(), policy="local_first", health=health)
    assert planner.select(make_request(), connectivity="online").name == "remote"

    with pytest.raises(NoRouteError):
        RoutePlanner(targets(), policy="remote_only").plan(
            make_request(remote_authorized="false"),
            connectivity="online",
        )
    with pytest.raises(TypeError, match="boolean"):
        RoutePlanner(
            targets(),
            allow_remote_when_connectivity_unknown="false",
        )


def test_target_output_cap_is_clamped_instead_of_removing_fallback() -> None:
    local = ProviderTarget(
        "local",
        "ollama",
        "small",
        False,
        languages=frozenset({"en"}),
        context_window=512,
        max_output_tokens=40,
    )
    planner = RoutePlanner((local,), policy="local_only")

    result = planner.plan(
        make_request(max_output_tokens=128),
        connectivity="online",
        estimated_input_tokens=100,
    )

    assert result == (local,)


def test_adaptive_remote_cascade_selects_one_tier_then_local() -> None:
    adaptive_targets = (
        ProviderTarget(
            "luna",
            "codex",
            "gpt-5.6-luna",
            True,
            languages=frozenset({"en"}),
            min_complexity_score=0,
        ),
        ProviderTarget(
            "terra",
            "codex",
            "gpt-5.6-terra",
            True,
            languages=frozenset({"en"}),
            min_complexity_score=3,
        ),
        ProviderTarget(
            "sol",
            "codex",
            "gpt-5.6-sol",
            True,
            languages=frozenset({"en"}),
            min_complexity_score=5,
        ),
        ProviderTarget(
            "local",
            "ollama",
            "small",
            False,
            languages=frozenset({"en"}),
        ),
    )
    planner = RoutePlanner(adaptive_targets, policy="remote_first")

    simple = planner.plan(make_request(), connectivity="online")
    medium = planner.plan(
        make_request(options={"complex": True}),
        connectivity="online",
    )
    complex_request = make_request(
        mode="think",
        messages=(
            ChatMessage(
                Role.USER,
                "explain A and B and C and D",
                ContentOrigin.RAW_TRANSCRIPT,
            ),
        ),
        options={"complex": True},
    )
    complex_result = planner.plan(complex_request, connectivity="online")

    assert [target.name for target in simple] == ["luna", "local"]
    assert [target.name for target in medium] == ["terra", "local"]
    assert [target.name for target in complex_result] == ["sol", "local"]


def test_auto_policy_reuses_adaptive_complexity_score() -> None:
    class CountingPlanner(RoutePlanner):
        complexity_calls = 0

        def complexity_score(
            self,
            request: ChatRequest,
            *,
            estimated_input_tokens: int | None = None,
            local_context_window: int | None = None,
        ) -> int:
            self.complexity_calls += 1
            return super().complexity_score(
                request,
                estimated_input_tokens=estimated_input_tokens,
                local_context_window=local_context_window,
            )

    planner = CountingPlanner(
        (
            ProviderTarget(
                "remote",
                "codex",
                "gpt-5.6-luna",
                True,
                min_complexity_score=0,
            ),
            ProviderTarget("local", "ollama", "small", False),
        ),
        policy="auto",
    )

    assert planner.plan(make_request(), connectivity="online")
    assert planner.complexity_calls == 1


def test_adaptive_remote_cascade_uses_next_healthy_tier() -> None:
    health = HealthTracker(failures_to_open=1)
    health.record_failure("codex/gpt-5.6-sol", ErrorCategory.CONNECTIVITY)
    planner = RoutePlanner(
        (
            ProviderTarget(
                "terra",
                "codex",
                "gpt-5.6-terra",
                True,
                min_complexity_score=3,
            ),
            ProviderTarget(
                "sol",
                "codex",
                "gpt-5.6-sol",
                True,
                min_complexity_score=5,
            ),
            ProviderTarget("local", "ollama", "small", False),
        ),
        policy="remote_first",
        health=health,
    )
    request = make_request(
        mode="think",
        messages=(
            ChatMessage(
                Role.USER,
                "explain A and B and C and D",
                ContentOrigin.RAW_TRANSCRIPT,
            ),
        ),
        options={"complex": True},
    )

    assert [target.name for target in planner.plan(request, connectivity="online")] == [
        "terra",
        "local",
    ]


def test_adaptive_remote_cascade_tie_keeps_only_first_candidate() -> None:
    planner = RoutePlanner(
        (
            ProviderTarget(
                "first-terra",
                "codex-a",
                "gpt-5.6-terra",
                True,
                min_complexity_score=3,
            ),
            ProviderTarget(
                "second-terra",
                "codex-b",
                "gpt-5.6-terra",
                True,
                min_complexity_score=3,
            ),
            ProviderTarget("local", "ollama", "small", False),
        ),
        policy="remote_first",
        mode_candidates={
            "talk": ("second-terra", "first-terra", "local"),
        },
    )

    result = planner.plan(
        make_request(options={"complex": True}),
        connectivity="online",
    )

    assert [target.name for target in result] == ["second-terra", "local"]


class FakeProvider:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


def test_registry_is_lazy_and_closes_only_owned_instances_once():
    built = []
    owned = FakeProvider()
    injected = FakeProvider()
    registry = ProviderRegistry()
    registry.register("owned", lambda: built.append("owned") or owned)
    registry.register_instance("injected", injected)

    assert built == []
    assert registry.get("owned") is owned
    assert registry.get("owned") is owned
    assert registry.get("injected") is injected
    assert built == ["owned"]

    registry.close()
    registry.close()
    assert owned.close_count == 1
    assert injected.close_count == 0
    with pytest.raises(RuntimeError):
        registry.get("owned")


def test_registry_closes_an_owned_prebuilt_instance_even_when_unused():
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register_instance("provider", provider, owned=True)

    registry.close()

    assert provider.close_count == 1


def test_input_estimate_includes_utf8_bytes_and_chat_framing():
    ascii_request = make_request(
        messages=(ChatMessage(Role.USER, "a", ContentOrigin.RAW_TRANSCRIPT),)
    )
    emoji_request = make_request(
        messages=(ChatMessage(Role.USER, "😀", ContentOrigin.RAW_TRANSCRIPT),)
    )
    two_messages = make_request(
        messages=(
            ChatMessage(Role.SYSTEM, "x", ContentOrigin.STATIC_INSTRUCTION),
            ChatMessage(Role.USER, "y", ContentOrigin.RAW_TRANSCRIPT),
        )
    )

    assert RoutePlanner.estimate_input_tokens(ascii_request) == 25
    assert RoutePlanner.estimate_input_tokens(emoji_request) == 28
    assert RoutePlanner.estimate_input_tokens(two_messages) == 42
