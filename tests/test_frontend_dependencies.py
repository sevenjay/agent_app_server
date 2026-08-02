from html.parser import HTMLParser
from pathlib import Path

EXPECTED_CDN_DEFER = {
    "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js": False,
    "https://unpkg.com/marked@15.0.12/marked.min.js": False,
    "https://unpkg.com/dompurify@3.2.6/dist/purify.min.js": False,
    "https://unpkg.com/alpinejs@3.14.9/dist/cdn.min.js": True,
}


class ScriptTagParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "script":
            self.scripts.append(dict(attrs))


def test_cdn_scripts_are_pinned_and_use_sri() -> None:
    parser = ScriptTagParser()
    parser.feed(Path("static/index.html").read_text(encoding="utf-8"))
    scripts = {
        script["src"]: script
        for script in parser.scripts
        if script.get("src", "").startswith("https://unpkg.com/")
    }

    assert set(scripts) == set(EXPECTED_CDN_DEFER)
    for source, expected_defer in EXPECTED_CDN_DEFER.items():
        attributes = scripts[source]
        assert attributes.get("integrity", "").startswith("sha384-")
        assert attributes.get("crossorigin") == "anonymous"
        assert ("defer" in attributes) is expected_defer


def test_console_uses_local_javascript_without_application_bundler() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    package = Path("package.json").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert 'src="/static/js/codex-console.js"' in html
    assert 'x-data="codexConsole()"' in html
    assert 'x-init="init()"' not in html
    assert "new EventSource(" in javascript
    assert "this.busy = false;" in javascript
    assert "liveUsage" in javascript
    assert "async newProject()" in javascript
    assert 'selected_thread_id: null' in javascript
    assert "(available[0]" not in javascript
    assert "await this.restoreThread(preferences.selected_thread_id)" in javascript
    assert ".innerHTML =" not in javascript
    assert "React" not in package
    assert "Vue" not in package
    assert '@source "../js/**/*.js";' in tailwind
    assert "overflow-hidden" in html
    assert "min-w-0" in html


def test_usage_panel_renders_structured_token_metrics() -> None:
    template = Path("templates/_thread_inspector.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert 'x-text="liveUsage"' not in template
    assert "Session total" in template
    assert "Latest request" in template
    assert "Cached input" in template
    assert "Reasoning" in template
    assert "normalizeUsage(data = {})" in javascript
    assert "this.liveUsage = this.normalizeUsage(event.data);" in javascript
    assert 'new Intl.NumberFormat("en-US"' in javascript


def test_timeline_live_debug_and_latest_changes_are_separate_tabs() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert 'role="tablist"' in html
    assert 'id="conversation-tab-timeline"' in html
    assert 'id="conversation-tab-debug"' in html
    assert 'id="conversation-tab-changes"' in html
    assert 'x-show="conversationTab === \'timeline\'"' in html
    assert 'id="live-debug"' in html
    assert 'x-show="conversationTab === \'debug\'"' in html
    assert 'id="latest-changes"' in html
    assert 'x-show="conversationTab === \'changes\'"' in html
    assert html.index("Live debug") < html.index("Latest changes")
    assert "No live debug events yet." in html
    assert "max-h-56" not in html
    assert 'conversationTab: "timeline"' in javascript
    assert 'this.conversationTab = "timeline";' in javascript


def test_files_tab_provides_lazy_tree_and_guarded_file_operations() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert 'id="conversation-tab-files"' in html
    assert 'id="project-files"' in html
    assert 'x-show="conversationTab === \'files\'"' in html
    assert html.index("Latest changes") < html.index('>\n              Files\n')
    assert "Upload files" in html
    assert "New folder" in html
    assert ">Refresh</button>" in html
    assert 'x-for="entry in visibleFileEntries"' in html
    assert 'selectAndToggleFileFolder(entry)' in html
    assert 'class="files-selection"' in html
    assert 'entry.path === fileSelectedPath' in html
    assert 'selectProjectFile(entry)' in html
    assert 'class="file-tree-actions"' not in html
    assert '>Download</button>' in html
    assert ':disabled="fileOperationBusy"' in html
    assert "async loadFileDirectory(path)" in javascript
    assert 'if (!Object.hasOwn(this.fileDirectories, entry.path))' in javascript
    assert "async uploadProjectFiles(event)" in javascript
    assert "async downloadProjectFile(entry = this.selectedFileEntry)" in javascript
    assert 'this.projectFilesUrl("/download", { path: entry.path })' in javascript
    assert 'link.download = entry.name;' in javascript
    assert 'error.code !== "file_exists"' in javascript
    assert "async newProjectFolder()" in javascript
    assert "async renameProjectFile(entry)" in javascript
    assert "async deleteProjectFile(entry)" in javascript
    assert "allowDuringOperation: true" in javascript
    assert "this.showFileError(error);" in javascript
    assert ".file-tree-row" in tailwind
    assert ".files-error" in tailwind


def test_latest_changes_use_a_readable_diff_view() -> None:
    template = Path("templates/_thread_changes.html").read_text(encoding="utf-8")
    inspector = Path("templates/_thread_inspector.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert "Latest changes" in template
    assert "Latest changes" not in inspector
    assert 'x-for="(line, index) in diffLines(liveDiff)"' in template
    assert 'class="diff-line"' in template
    assert "recorded-change" in template
    assert "diffLines(source)" in javascript
    assert "diffStats(source)" in javascript
    assert ".diff-line-addition" in tailwind
    assert ".diff-line-deletion" in tailwind


def test_timeline_file_changes_link_to_the_changes_tab() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")

    assert "View the full diff in Latest changes." in template
    assert "View Latest changes" in template
    assert 'aria-controls="latest-changes"' in template
    assert "conversationTab = 'changes'" in template
    assert '<pre class="diff-block">{{ change|tojson(indent=2) }}</pre>' not in template


def test_message_cards_render_sanitized_markdown_without_labels() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert template.count('class="markdown-body"') == 2
    assert template.count('x-html="renderMarkdown($el.dataset.markdown)"') == 2
    assert '<p class="item-label">You</p>' not in template
    assert '<p class="item-label">Codex</p>' not in template
    assert "window.marked.parse(markdown" in javascript
    assert "window.DOMPurify.sanitize(html" in javascript
    assert 'FORBID_ATTR: ["style"]' in javascript
    assert ".markdown-body {" in tailwind
    assert ".message-card .item-label" not in tailwind
    assert "background: oklch(0.255 0.05 195);" in tailwind
    assert "background: oklch(0.22 0.04 270);" in tailwind


def test_agent_message_deltas_stream_into_timeline() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert 'event.method === "item/agentMessage/delta"' in javascript
    assert "this.queueAgentMessageDelta(event);" in javascript
    assert "liveTimelineItems: []" in javascript
    assert "this.liveTimelineItems[index].text += segment.delta;" in javascript
    assert "this.clearCompletedLiveMessages(completedTurnId);" in javascript
    assert 'x-for="item in liveTimelineItems"' in template
    assert 'x-html="renderMarkdown(item.text)"' in template
    assert 'x-show="!liveTimelineItems.length"' in template
    assert "streaming-indicator" in tailwind
    assert "streaming-markdown::after" in tailwind


def test_goal_completion_reconciles_live_responses_with_persisted_history() -> None:
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    goal_idle = javascript.split('event.type === "console.goal.idle"', 1)[1].split(
        'event.type === "console.goal.error"',
        1,
    )[0]

    assert "const completedTurnId = event.turn_id;" in goal_idle
    assert "this.finishStreamingAgentMessages(completedTurnId);" in goal_idle
    assert "this.refreshThreadAndList()" in goal_idle
    assert "this.clearCompletedLiveMessages(completedTurnId);" in goal_idle


def test_user_messages_render_optimistically_before_live_responses() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    submit_start = javascript.index("async submitPrompt()")
    submit_end = javascript.index("async steerTurn(text)", submit_start)
    submit = javascript[submit_start:submit_end]
    steer_end = javascript.index("async interruptTurn()", submit_end)
    steer = javascript[submit_end:steer_end]

    assert submit.index("this.appendOptimisticUserMessage(text)") < submit.index("await this.api(")
    assert steer.index("this.appendOptimisticUserMessage(text)") < steer.index("await this.api(")
    assert "this.removeLiveMessage(messageKey);" in submit
    assert "this.removeLiveMessage(messageKey);" in steer
    assert 'kind: "user"' in javascript
    assert 'kind: "agent"' in javascript
    assert ":class=\"item.kind === 'agent' ? 'message-agent' : 'message-user'\"" in template
    assert ":class=\"{ 'streaming-markdown': item.kind === 'agent' && item.streaming }\"" in template


def test_only_the_current_agent_segment_keeps_its_streaming_cursor() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert "this.setStreamingAgentMessage(segment.key);" in javascript
    assert "streaming: true" in javascript
    assert "this.finishStreamingAgentMessages();" in javascript
    assert "this.finishStreamingAgentMessages(completedTurnId);" in javascript
    assert "this.finishStreamingAgentMessages(event.turn_id);" in javascript
    assert 'x-show="item.streaming" class="streaming-indicator"' in template
    assert ":aria-busy=\"item.kind === 'agent' && item.streaming\"" in template


def test_completed_tool_results_render_in_the_live_timeline() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert 'event.method === "item/completed"' in javascript
    assert "this.recordCompletedToolItem(event);" in javascript
    assert 'kind: "tool"' in javascript
    assert "tool: { ...item }" in javascript
    assert "this.liveResponseSegment += 1;" in javascript
    assert "liveToolOutput(tool = {})" in javascript
    assert "tool.aggregated_output ?? tool.aggregatedOutput" in javascript
    assert "tool.result != null" in javascript
    assert "tool.content_items ?? tool.contentItems" in javascript
    assert "item.kind === 'tool' && item.tool.type === 'commandExecution'" in template
    assert "item.kind === 'tool' && item.tool.type === 'fileChange'" in template
    assert "item.kind === 'tool' && item.tool.type === 'webSearch'" in template
    assert "!['commandExecution', 'fileChange', 'webSearch'].includes(item.tool.type)" in template
    assert 'x-text="liveToolOutput(item.tool)"' in template


def test_timeline_web_search_keeps_raw_type_and_only_formats_action() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    html = Path("static/index.html").read_text(encoding="utf-8")

    assert '<summary class="cursor-pointer item-label">{{ item_type }}</summary>' in template
    assert '<summary class="cursor-pointer item-label" x-text="item.tool.type"></summary>' in template
    assert 'x-for="entry in liveToolActionEntries(item.tool)"' in template
    assert "const action = tool.action?.root || tool.action;" in javascript
    assert 'const preferredOrder = ["type", "query", "queries", "url", "pattern"];' in javascript
    assert '.join(" · ")' in javascript
    assert "grid-cols-[max-content_minmax(0,1fr)]" in template
    assert 'class="contents"' in template
    assert "text-[10px] leading-5" in template
    assert "text-xs leading-5" in template
    assert "liveToolLabel(tool = {})" not in javascript
    assert 'x-text="eventText(event)"' in html


def test_collapsible_tool_cards_close_after_a_turn_ends() -> None:
    template = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    history = template.split('<template x-for="item in liveTimelineItems"', 1)[0]
    assert '<details class="tool-card" open>' not in history
    assert '<details class="tool-card">' in history
    assert '<details class="tool-card" open>' in template
    assert '<details class="tool-card overflow-hidden" open>' in template
    assert 'querySelectorAll("#timeline details.tool-card[open]")' in javascript
    assert "card.open = false;" in javascript
    assert javascript.count("this.collapseToolCards();") == 3


def test_goal_panel_and_slash_command_use_dedicated_goal_api() -> None:
    inspector = Path("templates/_thread_inspector.html").read_text(encoding="utf-8")
    composer = Path("templates/_composer.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert 'id="goal-panel"' in inspector
    assert "syncGoalSnapshot({{ thread.id|tojson }}" in inspector
    assert 'x-text="liveGoal.objective"' in inspector
    assert 'x-text="goalStatusLabel"' in inspector
    assert "Token budget" in inspector
    assert "startGoalFromEditor()" in inspector
    assert "pauseGoal()" in inspector
    assert "resumeGoal()" in inspector
    assert "clearGoal()" in inspector
    assert "activeKind === 'goal'" in composer

    submit_start = javascript.index("async submitPrompt()")
    active_branch = javascript.index("if (this.active)", submit_start)
    goal_branch = javascript.index('text === "/goal"', submit_start)
    assert goal_branch < active_branch
    assert "async handleGoalCommand(command)" in javascript
    assert "async showGoal()" in javascript
    assert "async startGoal(objective, tokenBudget = null)" in javascript
    assert 'method: "DELETE"' in javascript
    assert 'event.method === "thread/goal/updated"' in javascript
    assert 'event.type === "console.goal.idle"' in javascript
    assert ".goal-card" in tailwind
    assert ".goal-status-active" in tailwind


def test_split_headers_leave_conversation_at_the_top_of_the_app_shell() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    main_start = html.index('<main class="app-layout')
    sidebar_start = html.index('<aside', main_start)
    conversation_start = html.index('<section\n          class="conversation-panel', sidebar_start)
    inspector_start = html.index('<aside', conversation_start)

    assert html.index('<header class="app-header">', sidebar_start) < conversation_start
    assert html.index('<header class="app-header">', inspector_start) > inspector_start
    assert '<header class="app-header">' not in html[:main_start]
    assert "viewport-fit=cover" in html
    assert 'class="mobile-conversation-status lg:hidden"' in html
    assert "padding-bottom: env(safe-area-inset-bottom);" in tailwind
    assert ".panel-scroll {" in tailwind


def test_mobile_navigation_lists_sessions_before_chat() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")
    mobile_nav = html[html.index('<nav class="mobile-nav') :]

    assert mobile_nav.index(">Sessions</button>") < mobile_nav.index(">Chat</button>")
    assert mobile_nav.index(">Chat</button>") < mobile_nav.index(">Plan</button>")
    assert mobile_nav.index(">Plan</button>") < mobile_nav.index(">Status</button>")
    assert "repeat(4, 1fr)" in tailwind
    assert "mobileTab !== 'plan' && mobileTab !== 'status'" in html


def test_mobile_status_separates_usage_and_runtime_health_from_plan() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    inspector = Path("templates/_thread_inspector.html").read_text(encoding="utf-8")

    assert 'id="plan-panel-content"' in inspector
    assert "mobileTab === 'status'" in inspector
    assert 'id="usage-panel-content"' in inspector
    assert "mobileTab !== 'status'" in inspector
    assert 'hx-get="/partials/codex/status"' in html
    runtime_panel = html.split('hx-get="/partials/codex/status"', 1)[1]
    assert "mobileTab !== 'status'" in runtime_panel
    assert ">Goal</button>" not in html
    assert '<p class="section-kicker">Goal</p>' in inspector


def test_inspector_shows_compact_session_details() -> None:
    template = Path("templates/_thread_inspector.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert ">Session</p>" in template
    assert ">ID</dt>" not in template
    assert ">Pinned</dt>" not in template
    assert ">Model</dt>" in template
    assert 'x-text="currentModelId || \'default\'"' in template
    assert 'x-text="sessionStatusLabel"' in template
    assert "syncSessionStatus({{ thread.id|tojson }}" in template
    assert "currentModelLabel" not in template
    assert "get currentModelId()" in javascript
    assert "get currentModelLabel()" not in javascript
    assert "get sessionStatusLabel()" in javascript
    assert "item.is_default || item.isDefault" in javascript
    assert "this.activeModel = this.model || \"\";" in javascript
    assert '>Reasoning</dt>' in template
    assert 'x-text="currentReasoningEffortLabel"' in template


def test_goal_uses_and_displays_the_selected_model() -> None:
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    resume_goal = javascript.split("async resumeGoal()", 1)[1].split(
        "async clearGoal(",
        1,
    )[0]

    assert "const requestedModel = this.model || this.currentModelId || null;" in javascript
    assert "model: requestedModel" in javascript
    assert 'status: "active"' in resume_goal
    assert "model: requestedModel" in resume_goal
    assert 'this.activeModel = result.model || requestedModel || "";' in javascript
    assert 'Object.hasOwn(event.data || {}, "model")' in javascript


def test_goal_uses_and_displays_the_selected_reasoning_effort() -> None:
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert "this.reasoningEffort || this.defaultReasoningEffort || null" in javascript
    assert "reasoning_effort: requestedReasoningEffort" in javascript
    assert "this.activeReasoningEffort = requestedReasoningEffort" in javascript
    assert "result.reasoning_effort || requestedReasoningEffort" in javascript
    assert 'Object.hasOwn(event.data || {}, "reasoning_effort")' in javascript


def test_reasoning_effort_options_follow_the_selected_model_catalog() -> None:
    composer = Path("templates/_composer.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert composer.count('aria-label="Reasoning effort"') == 1
    assert 'x-model="reasoningEffort"' in composer
    assert 'x-for="option in reasoningEffortOptions"' in composer
    assert ':disabled="!reasoningEffortOptions.length"' in composer
    assert "supported_reasoning_efforts" in javascript
    assert "default_reasoning_effort" in javascript
    assert 'xhigh: "Extra high"' in javascript
    assert 'max: "Max"' in javascript
    assert 'ultra: "Ultra"' in javascript
    assert "Max and Ultra consume usage limits faster." in javascript
    assert "reasoning_effort: this.reasoningEffort || null" in javascript


def test_model_settings_use_a_compact_summary_and_comfortable_popover() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    composer = Path("templates/_composer.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert 'class="model-settings-trigger"' not in html
    assert composer.count('class="model-settings-trigger"') == 1
    assert composer.count('class="model-settings-popover"') == 1
    assert 'modelSettingsOpen: false' in javascript
    assert 'x-data="{ settingsOpen: false }"' not in composer
    assert composer.count('@click.away="modelSettingsOpen = false"') == 1
    assert composer.count('x-ref="settingsTrigger"') == 1
    assert composer.count('x-ref="modelSelect"') == 1
    assert composer.count('@keydown.escape.window="if (modelSettingsOpen)') == 1
    assert 'x-text="selectedModelLabel"' in composer
    assert 'x-text="selectedReasoningEffortLabel"' in composer
    assert 'id="composer-model-settings-panel"' in composer
    assert 'x-show="modelSettingsOpen"' in composer
    assert 'style="display: none;"' in composer
    assert "async selectThread(threadId) {\n      this.modelSettingsOpen = false;" in javascript
    assert ".model-settings-trigger" in tailwind
    assert ".model-settings-popover" in tailwind
    assert ".model-settings-field" in tailwind
    assert ".composer-model-settings .model-settings-popover" in tailwind
    assert ".model-controls" not in tailwind


def test_composer_floats_auto_grows_and_keeps_controls_inside() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    composer = Path("templates/_composer.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert 'class="composer-dock"' in html
    assert 'x-init="$nextTick(() => observeComposerDock($el))"' in html
    assert 'class="composer-surface"' in composer
    assert 'class="composer-toolbar"' in composer
    assert 'class="composer-input pretty-scrollbar"' in composer
    assert 'rows="1"' in composer
    assert '@input="resizeComposer($el)"' in composer
    assert '$watch("prompt"' in composer
    assert "resizeComposer(textarea)" in javascript
    assert "observeComposerDock(composer)" in javascript
    assert "new ResizeObserver(updateClearance)" in javascript
    assert 'class="composer-submit"' in composer
    assert composer.index('class="model-settings-trigger"') < composer.index(
        'class="composer-submit"'
    )
    assert "Live stream ready" not in composer
    assert "Ctrl/⌘ + Enter" not in composer
    assert ".composer-dock" in tailwind
    assert '.conversation-panel > [role="tabpanel"]::after' in tailwind
    assert "height: var(--composer-clearance, 8rem);" in tailwind
    assert ".composer-surface:focus-within" in tailwind
    assert "padding: 0.75rem clamp(1rem, 2.25vw, 2rem) 1.25rem;" in tailwind
    assert "background: rgb(0 0 0 / 50%);" in tailwind
    assert "max-width: 52rem;" not in tailwind
    assert "overflow-y: hidden;" in tailwind


def test_session_actions_are_available_from_each_session_row() -> None:
    thread_list = Path("templates/_thread_list.html").read_text(encoding="utf-8")
    inspector = Path("templates/_thread_inspector.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    tailwind = Path("static/src/input.css").read_text(encoding="utf-8")

    assert 'aria-label="Session actions"' in thread_list
    assert 'x-show="actionsOpen"' in thread_list
    assert 'role="menu"' in thread_list
    assert 'role="menuitem"' in thread_list
    assert "Rename" in thread_list
    assert '"Unpin" if thread.pinned else "Pin"' in thread_list
    assert "Fork" in thread_list
    assert "Archive" in thread_list
    assert "Delete session" in thread_list
    assert '<p class="session-actions-info-label">Info</p>' in thread_list
    assert '<p class="session-actions-info-title">Session ID</p>' in thread_list
    assert 'class="session-actions-info-id"' in thread_list
    assert '<span class="truncate font-mono">{{ thread.id }}</span>' not in thread_list
    assert ">Actions</p>" not in inspector
    assert "async renameThread(threadId = this.threadId)" in javascript
    assert "async archiveThread(threadId = this.threadId)" in javascript
    assert "async deleteThread(threadId = this.threadId)" in javascript
    assert 'method: "DELETE"' in javascript
    assert 'query.set("cache_bust", String(Date.now()));' in javascript
    delete_method = javascript.split("async deleteThread", 1)[1].split(
        "async unarchiveThread",
        1,
    )[0]
    assert "} finally {" in delete_method
    assert "await this.refreshThreads();" in delete_method
    assert ".session-actions-menu" in tailwind
    assert ".session-actions-info" in tailwind
    assert "width: min(13rem, calc(100% - 0.7rem));" in tailwind


def test_session_status_tracks_console_and_sdk_events() -> None:
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert 'sessionStatus: { type: "idle", activeFlags: [] }' in javascript
    assert 'event.method === "thread/status/changed"' in javascript
    assert 'event.data?.thread_id || event.data?.threadId || event.thread_id' in javascript
    assert 'this.syncSessionStatus(event.thread_id, "starting");' in javascript
    assert 'this.syncSessionStatus(event.thread_id, "running");' in javascript
    assert 'this.syncSessionStatus(event.thread_id, "stopping");' in javascript
    assert 'this.syncSessionStatus(event.thread_id, "idle");' in javascript
    assert 'this.syncSessionStatus(event.thread_id, "error");' in javascript
    assert '"waitingOnApproval"' in javascript
    assert '"waitingOnUserInput"' in javascript
    assert "normalizeSessionStatus(value = \"idle\")" in javascript
    assert "this.sessionStatus = this.normalizeSessionStatus(status);" in javascript


def test_user_facing_copy_uses_session_terminology() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    thread_list = Path("templates/_thread_list.html").read_text(encoding="utf-8")
    timeline = Path("templates/_thread_timeline.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")
    service = Path("codex_service.py").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "New thread" not in html
    assert "Select a thread" not in html
    assert "Thread details" not in html
    assert "Untitled thread" not in thread_list + timeline
    assert "This thread has no turns" not in timeline
    assert "Thread name (optional)" not in javascript
    assert "Archive this thread?" not in javascript
    assert '"The requested project or thread was not found."' not in service
    assert '"The thread state does not allow this operation."' not in service
    assert "## 術語約定" in readme
    assert "Web 前端面向使用者時則統一稱為 **Session**" in readme


def test_inspector_shows_three_plan_revisions() -> None:
    template = Path("templates/_thread_inspector.html").read_text(encoding="utf-8")
    javascript = Path("static/js/codex-console.js").read_text(encoding="utf-8")

    assert "Plan trajectory" in template
    assert (
        'x-init=\'syncPlanHistory({{ thread.id|tojson }}, '
        '{{ plan_history|tojson }})\''
    ) in template
    assert 'x-for="(plan, index) in livePlans"' in template
    assert "Revision ${index + 1}" in template
    assert ">Current</span>" in template
    assert "livePlan\"" not in template
    assert "recent.slice(-3)" in javascript
    assert "this.recordPlanUpdate(event);" in javascript
    assert "planStepMarker(status)" in javascript
