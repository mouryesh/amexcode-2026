/**
 * The only file that talks to the servicing API.
 *
 * The graph is stateless per turn: to confirm an action or choose between
 * ambiguous targets, the client resubmits the SAME message with an extra flag
 * set. That contract lives here so no component has to remember it.
 */

export interface Intent {
  /** Short label for a built flow, or "clarify" / "unmapped". */
  label: string;
  confidence: number;
  rationale?: string | null;
  /**
   * The canonical `domain.operation` id from specs/amex_in_catalog.yaml. This
   * is what actually routes — all 380 catalogue operations are reachable, while
   * `label` only names the handful with a built policy or tool. Show this
   * where there is room: "payflex_emi.foreclose_emi" tells the viewer what was
   * understood, "unmapped" does not.
   */
  operation?: string | null;
}

export interface Decision {
  outcome: "APPROVE" | "DECLINE" | "QUEUE" | "ESCALATE";
  reason_code: string;
  reason_text: string;
  policy_id: string;
  policy_version: string;
  source_tag?: string | null;
  source_ref?: string | null;
  rule_id?: string | null;
  inputs_used?: Record<string, unknown>;
}

export interface ActionTaken {
  tool?: string;
  action?: string;
  reference?: string;
  status?: string;
  tier?: string;
  detail?: string;
}

export interface AgentResponse {
  reply: string;
  actions_taken: ActionTaken[];
  decision_records: Decision[];
  escalate: boolean;
  escalation_packet?: Record<string, unknown> | null;
  intent?: Intent | null;
  awaiting_member: boolean;
}

export interface AuditRecord {
  record_id: number;
  trace_id: string | null;
  session_id: string | null;
  member_ref: string | null;
  event_type: "turn" | "policy" | "tool" | "security" | "terminal";
  actor: string;
  action: string;
  occurred_at: string;
  inputs: Record<string, unknown>;
  decision: Decision | null;
  prev_hash: string;
  record_hash: string;
}

export interface VerifyResult {
  status: "OK" | "TAMPERED";
  records?: number;
  chain_head?: string;
  broken_at_record_id?: number;
  reason?: string;
}

export interface Health {
  status: string;
  engine: string;
  target: string;
  splunk: boolean;
  sessions: string;
  splunk_stats: { shipped: number; dropped_full: number; dropped_error: number };
}

/** Flags that resubmit a message rather than sending a new one. */
export interface Resubmit {
  confirm?: boolean;
  reauthenticated?: boolean;
  selected_option?: number | null;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => fetch("/health").then(json<Health>),

  send: (
    sessionId: string,
    memberId: string,
    message: string,
    extra: Resubmit = {},
  ) =>
    fetch("/agent/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        member_id: memberId,
        message,
        confirm: false,
        reauthenticated: false,
        selected_option: null,
        ...extra,
      }),
    }).then(json<AgentResponse>),

  audit: (sessionId: string) =>
    fetch(`/audit/${sessionId}`).then(
      json<{ session_id: string; records: AuditRecord[] }>,
    ),

  verify: () => fetch("/audit/verify").then(json<VerifyResult>),
};

export const PERSONAS: { id: string; label: string; note: string }[] = [
  { id: "MEM-PRIYA", label: "Priya Sharma", note: "clean history · approves" },
  { id: "MEM-RAHUL", label: "Rahul Verma", note: "two prior waivers · declines" },
  { id: "MEM-ANANYA", label: "Ananya Rao", note: "stale income · queues" },
  { id: "MEM-VIKRAM", label: "Vikram Singh", note: "hardship · escalates" },
  { id: "MEM-DEEPA", label: "Deepa Iyer", note: "two cards · asks which" },
];

export const newSessionId = () => `s-${Math.random().toString(36).slice(2, 9)}`;
