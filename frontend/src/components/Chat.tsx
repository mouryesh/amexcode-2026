import { useEffect, useRef, useState } from "react";
import { api, type AgentResponse, type Resubmit } from "../api";

export interface Turn {
  role: "member" | "agent" | "system";
  text: string;
  data?: AgentResponse;
}

interface Props {
  sessionId: string;
  memberId: string;
  turns: Turn[];
  onTurns: (fn: (prev: Turn[]) => Turn[]) => void;
  onAudit: () => void;
}

/** Outcome drives the chip colour — it is the one place colour carries meaning. */
const outcomeClass = (o: string) =>
  o === "APPROVE" ? "chip ok" : o === "DECLINE" ? "chip no" : "chip hold";

/**
 * A reply that lists numbered options is a disambiguation question. Parsing it
 * back into buttons keeps the member from having to type "2" — and the numbers
 * are the server's, so nothing is invented client-side.
 */
function parseOptions(reply: string): string[] {
  return reply
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => /^\d+[.)]/.test(l));
}

export function Chat({ sessionId, memberId, turns, onTurns, onAudit }: Props) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const lastMessage = useRef<string>("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  async function send(message: string, extra: Resubmit = {}, echo?: string) {
    if (!message.trim() || busy) return;
    lastMessage.current = message;
    onTurns((p) => [...p, { role: "member", text: echo ?? message }]);
    setDraft("");
    setBusy(true);
    try {
      const data = await api.send(sessionId, memberId, message, extra);
      onTurns((p) => [...p, { role: "agent", text: data.reply, data }]);
      onAudit();
    } catch (e) {
      onTurns((p) => [
        ...p,
        { role: "system", text: `Request failed — ${(e as Error).message}` },
      ]);
    } finally {
      setBusy(false);
    }
  }

  const last = turns[turns.length - 1];
  const awaiting = last?.role === "agent" && last.data?.awaiting_member;
  const options = awaiting ? parseOptions(last.data!.reply) : [];

  return (
    <section className="pane">
      <h2>
        Conversation <span className="spacer" />
        <code>{sessionId}</code>
      </h2>

      <div className="log">
        {turns.length === 0 && (
          <p className="empty">
            Pick a cardmember and describe a request, the way they would.
          </p>
        )}

        {turns.map((t, i) => (
          <div key={i} className={`msg ${t.role}`}>
            <div className="bubble">{t.text}</div>
            {t.data && <Meta data={t.data} />}
          </div>
        ))}

        {awaiting && (
          <div className="choices">
            {options.length >= 2 ? (
              options.map((line, i) => (
                <button
                  key={i}
                  onClick={() =>
                    send(lastMessage.current, { selected_option: i + 1 }, `Option ${i + 1}`)
                  }
                >
                  {line}
                </button>
              ))
            ) : (
              <>
                <button
                  onClick={() => send(lastMessage.current, { confirm: true }, "Confirm")}
                >
                  Confirm
                </button>
                <button
                  className="ghost"
                  onClick={() =>
                    onTurns((p) => [
                      ...p,
                      { role: "system", text: "Cancelled. Nothing was executed." },
                    ])
                  }
                >
                  Cancel
                </button>
              </>
            )}
          </div>
        )}

        {busy && <p className="empty">Thinking…</p>}
        <div ref={endRef} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="e.g. can you waive my late fee?"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !draft.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}

/** Everything the agent decided, surfaced rather than buried in prose. */
function Meta({ data }: { data: AgentResponse }) {
  const chips: React.ReactNode[] = [];

  // Prefer the canonical catalogue operation over the short label: it says what
  // was actually understood. "unmapped" is a routing outcome, not an intent.
  if (data.intent)
    chips.push(
      <span key="i" className="chip intent" title={data.intent.rationale ?? undefined}>
        {data.intent.operation ?? data.intent.label} ·{" "}
        {(data.intent.confidence * 100).toFixed(0)}%
      </span>,
    );

  data.decision_records.forEach((d, i) => {
    chips.push(
      <span key={`o${i}`} className={outcomeClass(d.outcome)}>
        {d.outcome} · {d.reason_code}
      </span>,
      <span key={`p${i}`} className="chip muted">
        {d.policy_id} {d.policy_version}
      </span>,
    );
    // source_tag says whether the rule is a real published Amex rule or an
    // illustrative control — worth showing, not hiding.
    if (d.source_tag)
      chips.push(
        <span key={`s${i}`} className="chip muted">
          {d.source_tag}
        </span>,
      );
  });

  data.actions_taken.forEach((a, i) =>
    chips.push(
      <span key={`a${i}`} className="chip ok">
        {a.tool ?? a.action} → {a.reference ?? "done"}
      </span>,
    ),
  );

  if (data.escalate)
    chips.push(
      <span key="e" className="chip no">
        handed to a specialist
      </span>,
    );
  if (data.awaiting_member)
    chips.push(
      <span key="w" className="chip hold">
        awaiting you
      </span>,
    );

  return chips.length ? <div className="meta">{chips}</div> : null;
}
