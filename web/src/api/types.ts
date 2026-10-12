/**
 * DeepAgentCLI Web API TypeScript reference. Documentation contract only.
 * Copy/adapt into web/src/api/types.ts; actual responses must be checked
 * against the executing repository. No mock values are installed in runtime.
 */
export type RunStatus = 'running' | 'waiting_confirmation' | 'waiting_human' | 'paused' | 'runtime_config_boundary' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
export type TerminalRunStatus = 'completed' | 'failed' | 'cancelled' | 'interrupted';
export interface Project {
    id: string;
}
export interface Session {
    id: string;
    project_id: string;
    created_at: string;
}
export interface PendingToolCall {
    toolCallId: string; // Runner camelCase; approval request uses tool_call_id
    name: string;
    args: Record<string, unknown>;
}
export type HumanFieldType = 'text' | 'textarea' | 'single_select' | 'multi_select' | 'boolean';
export type InteractionType = 'clarification' | 'decision' | 'confirmation' | 'review';
export interface HumanOption {
    value: string;
    label: string;
    description: string;
    allowsCustomText?: boolean;
}
export interface HumanField {
    id: string;
    type: HumanFieldType;
    label: string;
    required: boolean;
    placeholder: string;
    options: HumanOption[];
}
export interface HumanInputRequest {
    type: 'human_input';
    interactionType: InteractionType;
    title: string;
    reason: string;
    question: string;
    fields: HumanField[];
    blocking: true;
    impact: string[];
    interactionId?: string; // NOT service interrupt_id
    recommendation?: {
        value: string;
        reason: string;
    };
}
export interface RunResult {
    status: string;
    output?: string;
    pending_tool_calls?: PendingToolCall[];
    human_input?: HumanInputRequest | Record<string, never>;
    error?: string;
}
export interface Run {
    id: string;
    session_id: string;
    project_id: string;
    status: RunStatus;
    created_at: string;
    updated_at: string;
    result: RunResult | Record<string, never>;
    interrupt_id: string | null;
}
export interface RunSummary {
    id: string;
    session_id: string;
    project_id: string;
    status: RunStatus;
    created_at: string;
    updated_at: string;
}
export interface PaginatedRuns {
    runs: RunSummary[];
    next_cursor: string | null;
}
export interface ActiveRunResponse {
    run: RunSummary | null;
    blocked?: boolean;
}
export interface AgentEvent {
    type: string;
    content: string;
    message_id: string;
    tool_call_id: string;
    name: string;
    arguments: Record<string, unknown>;
    result: unknown;
    is_error: boolean;
    stream: string;
    artifact: unknown;
}
export interface ToolDecision {
    tool_call_id: string;
    decision: 'approve' | 'reject';
    message?: string;
}
export interface ApprovalBody {
    interrupt_id: string;
    decisions: ToolDecision[];
}
export interface HumanInputBody {
    interrupt_id: string;
    values: Record<string, unknown>;
}
export interface ContinueBody {
    interrupt_id: string;
}
export interface DeltaEvent {
    kind: string;
    text: string;
}
export type TopLevelEventType = 'run_started' | 'delta' | 'agent_event' | 'interaction_requested' | 'interaction_submitted' | 'interaction_rejected' | 'run_finished';
export type RunSseFrame = {
    id: number;
    event: TopLevelEventType | string; // unknown events are forward-compatible
    data: unknown;
};
// The two new response shapes above (RunSummary/PaginatedRuns/ActiveRunResponse)
// are implemented by the authenticated M3 read-only service endpoints.
