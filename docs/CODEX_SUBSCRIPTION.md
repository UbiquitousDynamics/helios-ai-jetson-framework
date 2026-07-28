# Codex via ChatGPT subscription

Helios can use the same native mechanism used by OpenClaw: the official Codex
app-server runs locally over stdio and reuses the ChatGPT sign-in stored by
Codex. This route does not use `OPENAI_API_KEY`, does not call the billable
OpenAI API-key endpoint, and falls back to Ollama when Codex cannot run.

This is an optional remote route. The prompt still leaves the Jetson and is
processed by OpenAI under the terms and usage limits of the signed-in ChatGPT
account.

## What Helios enforces

- the child Codex process receives empty `OPENAI_API_KEY` and `CODEX_API_KEY`
  values, matching OpenClaw's protection against accidental API-key precedence;
- the account returned by app-server must have type `chatgpt`; no account or an
  `apiKey` account is rejected before a turn is started;
- every thread is ephemeral, uses an empty temporary working directory,
  read-only sandboxing and deny-all approvals;
- `CODEX_HOME` is isolated and receives only a private copy of `auth.json`, not
  the user's Codex configuration, MCP servers, plugins or connectors;
- shell, unified execution, web search, apps, plugins, skills and tool discovery
  are disabled for this provider;
- the normal Helios privacy authorization remains mandatory;
- authentication, connectivity, quota and startup failures remain eligible for
  the configured Ollama fallback;
- content and credentials are not written to Helios logs or metrics.

ChatGPT subscription usage is not the OpenAI API free tier. Availability,
models and message/compute limits depend on the ChatGPT plan and can change.
Helios therefore does not assign a monetary API price to this route.

## Jetson installation and login

From the existing virtual environment:

```bash
cd ~/helios-ai-jetson-framework
python -m pip install -r requirements-remote.txt
python scripts/codex_subscription.py login
```

The login command prints a verification URL and one-time code. Open the URL on
any browser, enter the code, and complete the ChatGPT sign-in. No API key is
needed. The official dependency includes an ARM64 Codex runtime for Jetson.

Check the resulting authentication method and the model catalog:

```bash
python scripts/codex_subscription.py status
python scripts/codex_subscription.py models
```

`status` must print:

```text
Codex account type: chatgpt
Helios subscription routing: ready
```

If it prints `apiKey`, Helios will reject that session. Removing API keys from
the parent shell is optional because Helios already clears both variables in
its Codex child:

```bash
unset OPENAI_API_KEY
unset CODEX_API_KEY
```

## Enable the OpenClaw-style route

Use the committed configuration:

```bash
export HELIOS_LLM_CONFIG="$PWD/examples/llm-routing.codex-subscription.toml"
export HELIOS_LLM_REMOTE_ENABLED=true
python main.py
```

The example is `remote_first`:

```text
Helios -> Codex app-server (stdio) -> ChatGPT account
       -> Ollama fallback when the first route is unavailable
```

The example currently selects `gpt-5.6-sol`. Model access is account-dependent.
Run `models`, choose an ID actually returned on the Jetson, and change both
`targets.codex-talk.model` and `targets.codex-think.model` if needed. Use lower
reasoning effort for `talk` and a higher value for `think`.

Do not add `api_key_env` to the `codex_app_server` provider. Configuration
validation intentionally rejects it.

## Understand which model answered

At request start, `Executing talk request using route codex-talk,local-talk`
shows the eligible routes in fallback order, not the route that eventually
answered. The completion line is authoritative:

```text
Completed talk request using route codex-talk (provider=openai-codex, ...)
Completed talk request using route local-talk (provider=ollama, ...)
```

The committed Codex profile gives remote speech 30 seconds to produce its first
spoken sentence before counting the successful turn against provider health.
The generic 1.5-second edge-model objective is too short for Codex and would
open its circuit after three otherwise successful turns. If a circuit does
open because of real failures, Helios logs its status and remaining cooldown;
restarting Helios also clears in-memory health state.

Both hybrid routes receive the same Emilia identity, response language,
direct-answer rule and 20-word `talk` limit. They are still different models,
so `remote_first` cannot guarantee identical wording or facts when fallback
occurs. Use `remote_only` or `local_only` when every answer must come from one
engine; keep `remote_first` when availability is more important.

## Verify before starting the voice assistant

Run the network-free tests:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_codex_app_server.py tests/test_config_llm.py -q
```

For one real remote-only certification request, copy the example outside the
repository and set `router.policy = "remote_only"`:

```bash
cp examples/llm-routing.codex-subscription.toml /tmp/helios-codex-live.toml
sed -i 's/policy = "remote_first"/policy = "remote_only"/' /tmp/helios-codex-live.toml
export HELIOS_LLM_LIVE=1
export HELIOS_LLM_LIVE_CONFIG=/tmp/helios-codex-live.toml
python -m pytest tests/test_live_llm.py -m remote_live -q
```

The test succeeds only if the recorded successful provider is not Ollama. To
see the configured order without exposing credentials:

```bash
python -c "import config; s=config.LLM_SETTINGS; print(s.routing_policy); print([(t.name, t.provider, t.model) for t in s.targets])"
```

## Configurable and deployment-owned decisions

These cannot be decided by the repository:

- which ChatGPT account and plan owns the Jetson deployment;
- which returned Codex model IDs are enabled for that account;
- whether plan usage limits are sufficient for the expected driving workload;
- whether sending voice transcripts to OpenAI is permitted by the privacy,
  consent, retention and regional requirements of the deployment;
- acceptable latency, mobile-data use and fallback behavior on the real Jetson;
- how interactive login renewal and account revocation are operated.

Benchmark the exact Jetson, modem and network conditions before enabling
remote-first in production. The immediate rollback remains:

```bash
export HELIOS_LLM_EMERGENCY_LOCAL_ONLY=true
```

Restart Helios after changing the emergency switch.
