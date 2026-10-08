# ChatGPT export import: spec for the test author

Written 2026-10-07 by the khipumaq owner (Claude Sonnet 5.5) for Codex to write the tests from, without seeing the
importer, which does not exist yet. Where this spec is wrong or silent, say so in the tests' preamble: a wrong spec
found by a test author is the point of the split.

## What this is for

Tony has two ChatGPT exports. Neither is in khipumaq. They disagree with each other in ways we cannot yet explain, so
the importer must keep both and decide nothing silently. The Claude equivalent is `src/khipumaq/claude_ai_export.py`
with `tests/test_claude_ai_export.py`; read the tests for style, not the module for answers.

## Invariants (each should be a failing test first)

1. **Additive only.** No record already stored is replaced or altered by an import: not an episode, not a raw
   document, not anything else. A document whose key exists is left exactly as it is, including `_rev`.
2. **Idempotent.** A second run of the same inputs adds nothing and reports `new: 0`.
3. **Opt-in, structurally.** Every episode has `experiment_label == "chatgpt-chat"` (already in
   `khipumaq.index.OPT_IN_LABELS`) and is stored in `index.CHAT`, never in `index.EPISODES`, so the default view
   (`index.VIEW`) cannot contain it: `search(scope="all")`, and a client running older code with no filter at all, never
   return it, and `search(scope="chatgpt-chat")` does (it routes to `index.CHAT_VIEW`). The importer calls
   `ensure_chat_index(db)` before writing. A test must show the default store is byte-identical before and after.
4. **Namespaced keys.** Episode `_key` starts with `chatgpt-` so it cannot meet a Code-session key or a `claude-ai-` one.
5. **Every branch.** ChatGPT's `mapping` is a tree (`parent` per node, `current_node` per conversation). Every node is
   imported, including nodes off the active path. An earlier importer elsewhere collapsed regenerations into one line
   and lost branches; a fork must produce one episode per assistant reply, each paired with the user message it
   answered by `parent` links, not by order.
6. **Both versions survive.** When the same message id appears in two exports with different content, both versions are
   kept in raw, distinguishable by export. The episode comes from the export imported first; the run reports the
   number of conflicts. Nothing is overwritten to resolve a conflict.
7. **Nothing invented.** A node with no usable parent is paired by time and the episode says so (`structure:
   "timestamp-order"`; `"linked"` otherwise). A missing field stays missing.
8. **Dry run writes nothing** and reports the same counts as a real run.
9. **No network, no live store.** Tests use the suite's disposable collections (see `tests/conftest.py`).

## Proposed interface (change it if the tests find it awkward)

- Module `khipumaq.chatgpt_export`: `chatgpt_episodes(conversation, export_id)`,
  `chatgpt_raw_documents(conversation, export_id)`, `ingest_chatgpt(db, paths, export_id, dry_run=False)`.
- CLI: `khipumaq import-chatgpt --export-id ID PATH [PATH ...] [--dry-run]`, one export per call, so the two exports are
  two calls with two ids. `PATH` is a `conversations.json` or the `conversations-NNN.json` shards of one export.
- Result: `{"episodes": {"new", "existing"}, "raw": {"new", "existing"}, "conflicts": N}`.
- Raw keys include the export id, so one message in two exports is two raw documents; episode keys do not, so it is one
  episode (invariant 6). Episodes go to `index.CHAT`, as the Claude importer's do (see `tests/test_claude_ai_export.py`).

## What the two exports are (measured 2026-10-05, read-only)

| | old: llm-data, 2025-12-26 | new: tmp, 2026-10-04 |
|---|---|---|
| files | one `conversations.json` | `conversations-000..009.json` shards |
| conversations | 764 | 943 |
| in both (by `id`) | 762 | 762 |
| only here | 2 | 181 |
| roles | user, assistant, **system, tool** | user, assistant |
| content types | text, code, thoughts, reasoning_recap, multimodal_text, tether_browsing_display, tether_quote, user_editable_context, execution_output, system_error | text, thoughts, reasoning_recap, multimodal_text |
| hidden nodes (`metadata.is_visually_hidden_from_conversation`) | 4,007 | 0 |
| nodes with `weight == 0` | 2,650 | 0 |
| message fields | has `recipient`, `status`, `weight`, `end_turn` | none of those four |

Among the 762 shared conversations: every one has a different node count (the old has extra system/tool/hidden nodes);
of 28,020 shared messages, 25 differ in `content`. A differential comparison (2026-10-07, before any import) found
that in every one of the 25 the text is identical in both exports (zero differing string parts); they differ only in the
`metadata` of attached asset parts (38 parts, on user `multimodal_text` messages, same `create_time`). So "first export
wins the episode" costs nothing in text; both raw versions are still kept (invariant 6).

Old-export assistant messages by type: 14,442 are `text` to recipient `all` (replies); 467 are `text` to `bio`
(ChatGPT's saved-memory writes, e.g. "Tony is exploring..."); the rest are tool calls (`python`, `web`, `web.run`) and
canvas documents (`canmore.create_textdoc`, `canmore.update_textdoc`).

Conversation keys: `id` (equals `conversation_id`), `title`, `create_time`, `update_time` (epoch seconds as floats),
`current_node`, `mapping`, `default_model_slug`, `is_archived`, `is_starred`, `memory_scope`, ... Node keys: `id`,
`parent`, `message` (null for each conversation's root; the old export's node keys were not inspected). Message keys: `id`, `author`
(`role`), `content` (`content_type`, `parts` or `text`), `create_time`, `metadata` (`model_slug`, `parent_id`, ...).

## Exclusion list

The CLI takes `--exclude-file PATH` (conversation ids, one per line, `#` comments allowed), as the Claude importer does.
An excluded conversation is not stored at all (no episode, no raw), and the result reports `excluded: N`, so a
conversation left out is declared and never silent. Tests: excluded ids absent from both collections; others present;
dry run and real run report the same; the count is in the result.

## Rules to test

- **Episode source.** An episode is an assistant message with content type `text`, not hidden, with non-empty text, and
  whose `recipient` is `all` (a reply, `kind: "reply"`) or `bio` (ChatGPT's saved-memory write about the user,
  `kind: "memory-write"`) when the field exists; the new export has no `recipient`, so every visible assistant `text`
  there is a reply. Memory writes are episodes on purpose: they are what another party said about the user, and the
  user does not want that kept from the ayllu (opt-in label still applies). `thoughts`, `reasoning_recap`, tool
  calls and canvas documents make no episode; they are in raw, stored and not hidden.
- **Prompt.** The nearest `user` ancestor by `parent`, skipping system/tool/hidden nodes. Its `parts` that are strings
  are the text; dict parts (images, asset pointers) contribute nothing to the text.
- **Fields on the episode**, as the Claude importer has them where they apply: `_key`, `session_id` (conversation id),
  `ts` (ISO from `create_time`), `model` (`metadata.model_slug` or null), `experiment_label`, `source_file`,
  `user_message`, `user_ts`, `response`, `state: {}`, `state_text: ""`, `activity_log: []`, `host`, `machine_id`,
  `agent_id`, `conversation_name` (title), `parent_uuid`, `structure`; plus `kind` (`reply` or `memory-write`), `active_path` (true when the node lies on the
  path from `current_node` to the root) and `export_id`.
- **Raw.** One document per node that has a message, plus one for each conversation's metadata (everything but
  `mapping`). `text` is the parsed object re-serialized as JSON; say in a test that it is not the original bytes.
- **Ordering.** No test should depend on dict order or on which of two equal timestamps comes first.

## What I do not know (please probe, and write a test pinning whatever you find)

1. (Resolved: see the comparison above. Please confirm it independently from the data, since one reading of mine
   already had to be corrected.)
2. Whether `time` values are ever `None` or strings, and what `ts` should be then.
3. Whether any conversation has a cycle in `parent` links, or a `current_node` that is not in `mapping`.
4. Whether `multimodal_text` parts can hold user prose in a dict form.
5. What `user_editable_context` (388 old nodes; custom instructions) should be: raw only is my proposal, and the
   user's principle (nothing hidden) argues for checking whether it is prose the user wrote.
6. Whether a huge pasted user message exists here as it does in Claude exports (cap: 20,000 characters, see
   `PASTE_LIMIT` in the Claude importer). I do not know, and do not want a cap added without a measured need.

## Out of scope

Third-party and client content (Tony may supply an exclusion list later), the Claude importer, and any change to
search. The data files are not in the repo: the new export is under `tmp/` (untracked) with a stamped SHA-256 manifest
(`tmp/hashes-2026-10-04.sha256`); the old one is under `~/projects/llm-data/chatgpt/`, unhashed. Do not copy either
into the repo or print message text into any report: counts and ids only.
