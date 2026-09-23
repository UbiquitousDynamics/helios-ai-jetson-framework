# Live conversation: final implementation audit

This audit reflects the worktree after Tasks 00–15. Ratings describe verified
behavior, not product parity. The original source titles and baseline assessment
remain in [the progress checkpoint](live-conversation-progress.md). `supported`
means the implemented subset meets the stated requirement as written; `partial`
means a material part is missing or has only synthetic evidence.

## Architecture and migration

The recognizer emits provisional revisions and authoritative finals. A local
endpoint detector and one realtime controller own capture and floor state.
Confirmed finals alone reach the API, RAG, task and history boundaries. During
generation and playback, a plausible interruption ducks Piper; a confirmed
final cancels the response. The API retains bounded provider-neutral history,
including failed and interrupted turns. A separate, optional `TaskDelegator`
executes injected fake work with bounded task state, explicit confirmation,
content-free status and causal replacement. It does not infer tasks from speech
or invoke external services.

Existing embedders need no change: the optional `VoiceAssistant.task_delegator`
argument defaults to `None`; the public `run_once` and `process_command` paths
remain. Embedders that opt into fake delegation provide a `TaskDelegator`, call
`delegate_task`, `confirm_task`, or `steer_task`, and close their owned delegator.
`CANCEL_TASK` targets the active delegated task when one exists. Configure the
wake-free window with `HELIOS_ACTIVATION_TIMEOUT_SECONDS` (default 30 seconds).
Speech output and task cancellation remain separate. Backchannels use cached
neutral cues, and spoken model answers receive a concise style instruction.

## FR-01 through FR-45

| ID | Requirement | Rating | Evidence and remaining gap |
| --- | --- | --- | --- |
| FR-01 | Continuous Voice Interaction | supported | `VoiceAssistant.run_once`; wake-free follow-ups in `tests/test_wake_sessions.py`. |
| FR-02 | Full-Duplex Interaction | partial | `RealtimeConversationController` and `tests/test_full_duplex_capture.py` cover synthetic generation/playback capture; deployed acoustic continuity is unmeasured. |
| FR-03 | Natural Interruption Handling | partial | `VoiceAssistant._capture_barge_in`, `PiperTTS.duck` and `tests/test_interruption_candidates.py`; speaker intent remains heuristic. |
| FR-04 | Pause Understanding | partial | `TurnEndpointDetector` and `tests/test_turn_endpoint_detector.py` bound pauses and finalization; semantic intent is unknown. |
| FR-05 | Backchannel Support | partial | `BackchannelSession` and `tests/test_backchannel_policy.py` verify delayed, suppressed cues; naturalness is unmeasured. |
| FR-06 | Conversational Floor Management | partial | `RealtimeConversationController` and `tests/test_realtime_conversation.py` track floor events; other-speaker attribution is absent. |
| FR-07 | Wake-Word Activation | supported | `VoiceAssistant.contains_wake_word`; `tests/test_assistant.py` covers whole-word activation. |
| FR-08 | Follow-Up Interaction Window | supported | `VoiceAssistant._voice_conversation_is_active`; `tests/test_wake_sessions.py` covers timeout and repeated follow-ups. |
| FR-09 | Explicit Session Termination | partial | `ControlIntent.END_SESSION` and `tests/test_control_intents.py` require reactivation; native cleanup is cooperative. |
| FR-10 | Addressed-Speech Detection | unsupported | `VoiceAssistant.run_once` uses activation and echo checks, without addressee detection. |
| FR-11 | Multi-Turn Context Management | supported | `ConversationSession.history_before`; `tests/test_conversation_continuity.py` covers bounded canonical history. |
| FR-12 | Mid-Utterance Self-Correction Handling | partial | `TranscriptRevisionAggregator` and `tests/test_transcripts.py` preserve final wording within a capture; separate native finals remain separate turns. |
| FR-13 | Mid-Task Steering | partial | `VoiceAssistant.steer_task` and `tests/test_task_delegation.py` replace explicit fake work and reject late results; speech intent is not mapped to a task. |
| FR-14 | Asynchronous Task Delegation | partial | `TaskDelegator.delegate` and `tests/test_task_delegation.py` keep fake work off the voice thread; real services are absent. |
| FR-15 | Task-State Management | supported | `TaskManager`, `TaskSnapshot` and `tests/test_task_manager.py` cover seven states, stable IDs, bounds and races. |
| FR-16 | Verified Action Reporting | partial | `TaskManager.transition` requires confirmation and `TaskDelegator.status` reports confirmed fake completion; no real action adapter exists. |
| FR-17 | Multimodal Intent Detection | unsupported | `VoiceAssistant._process_model_prompt` has no sensor intent classifier. |
| FR-18 | Egocentric Visual Perception | unsupported | `VoiceAssistant.__init__` has no image/video perception input. |
| FR-19 | Visual Deictic Reference Resolution | unsupported | `ConversationSession` stores textual turns without scene grounding. |
| FR-20 | Continuous Visual Assistance | unsupported | `VoiceAssistant.run` has no continuous vision mode or consent lifecycle. |
| FR-21 | Visual Change Detection | unsupported | No scene/frame events or visual delta model exists. |
| FR-22 | Short-Term Conversational Memory | partial | `ConversationSession` retains bounded active-session turns; no persistent task facts or evicted-fact retrieval. |
| FR-23 | Persistent User Memory | unsupported | `ConversationSession.reset` clears in-process state; no durable consent-controlled store. |
| FR-24 | Episodic Memory | unsupported | `ConversationTurn` does not represent spatial or perceptual episodes. |
| FR-25 | Contextual Memory Retrieval | unsupported | `ConversationSession.history_before` selects recent turns; RAG searches an explicit local corpus only. |
| FR-26 | Memory Consent and Control | unsupported | No persistent memory exists to inspect, restrict or delete. |
| FR-27 | Tool Integration Framework | unsupported | `TaskDelegator` accepts injected fake work; no standardized real-service dispatcher exists. |
| FR-28 | Action Confirmation Policy | partial | `TaskDelegator.confirm` gates a fake commit; no sensitivity classifier or real action policy exists. |
| FR-29 | Proactive Assistance | unsupported | `VoiceAssistant.run_once` responds to input; no opt-in proactive scheduler. |
| FR-30 | Context-Aware Proactivity | unsupported | No proactive urgency/relevance/interruption-cost policy exists. |
| FR-31 | Multi-Speaker Handling | partial | `ConservativeEchoSuppressionPolicy` filters likely self-echo; no speaker identity or diarization. |
| FR-32 | Noise Robustness | partial | `BargeInDetector` and `tests/test_echo_suppression_policy.py` cover synthetic noise/echo cases; field disturbances are unmeasured. |
| FR-33 | Cross-Device Conversation Continuity | unsupported | `ConversationSession` is single-process; no identity, sync or handoff transport. |
| FR-34 | Graceful Connectivity Degradation | partial | `APIClient` gated fallback and `tests/test_hybrid_api_client.py`; user-facing capability-specific notices are absent. |
| FR-35 | Local Basic Commands | partial | `parse_control` and `tests/test_control_intents.py` cover stop, mute, session and task controls; volume and spoken privacy controls are absent. |
| FR-36 | Conversation and Task Synchronization | partial | `TaskManager` rejects late fake results and `ConversationSession` tracks turns; no real tool-result synchronization protocol. |
| FR-37 | Provisional and Authoritative Transcript Management | partial | `api.transcripts` and `tests/test_transcripts.py` guard dispatch; identity-free legacy finals cannot always be deduplicated. |
| FR-38 | Dynamic Context Management | supported | `ConversationSession._trim_locked`; `tests/test_conversation_continuity.py` verifies bounded retention without resetting logical turns. |
| FR-39 | Multilingual Conversation | partial | `LanguageProfile` supports Italian and English at construction; active-session switching is absent. |
| FR-40 | Live Translation | unsupported | No bidirectional speaker-aware translation pipeline. |
| FR-41 | Personality Continuity | partial | `LanguageProfile` and cached cues stabilize session voice/style; durable preferences are absent. |
| FR-42 | Conversation-Aware Response Length | partial | `spoken_response_instruction` requests concise spoken answers and expansion as needed; model compliance is not guaranteed. |
| FR-43 | Long-Running Task Feedback | partial | `TaskDelegator.status` gives sparse content-free state; automatic spoken progress scheduling is absent. |
| FR-44 | Sensor Privacy Awareness | partial | Local mute and capture ownership exist; user-visible microphone/camera indicators are absent. |
| FR-45 | Context-Dependent Sensor Activation | partial | `SpeechRecognizer.prepare_async` avoids capture until listening; no general sensor/world-state activation policy. |

## Validation and deferred certification

The final local suite and Emilia synthetic suite results are recorded in the
progress checkpoint. Emilia testing used the stored SSH host key, an isolated
copy, fake backend tasks, and no physical microphone, speaker, live provider,
user speech or external action. Its measured focused task gate reports wall
3.114 s, CPU user/system 2.944/0.140 s, and peak child RSS 47,640 KiB.
These measurements do not certify acoustic quality, end-to-end first-audio or
interruption latency on the deployed enclosure, long-run memory/thermal/power
behavior, or production network conditions. Those require calibrated field
fixtures, sustained Jetson profiling, and separate approval for real services.
Persistent memory, vision/world state, diarization, cross-device handoff,
dynamic language switching, live translation, real tools, and proactive
assistance remain separate optional tracks.
