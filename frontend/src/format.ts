// Display-only helpers: formatting and labels. Nothing here decides or computes money.

export function clock(iso: string): string {
  const d = new Date(iso);
  const hms = d.toLocaleTimeString("en-GB", { hour12: false });
  return `${hms}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

export const grouped = (n: number) => n.toLocaleString("en-GB");

const ACCOUNTS: Record<string, string> = {
  SENDER_ACCOUNT: "Sender's UK bank account",
  UK_COLLECTION_ACCOUNT: "UK collection account",
  GHS_FX_POOL: "Ghana cedi FX pool",
  GH_SETTLEMENT_ACCOUNT: "Ghana settlement account",
  RECIPIENT_ENDPOINT: "Recipient's mobile money wallet",
};

export const accountName = (code: string) => ACCOUNTS[code] ?? code;

const STATES: Record<string, string> = {
  FAILED: "Failed",
  DIAGNOSING: "Diagnosing",
  AWAITING_APPROVAL: "Awaiting approval",
  APPROVED: "Approved",
  RECOVERY_EXECUTING: "Recovery executing",
  RECOVERED: "Recovered",
  RECONCILED: "Reconciled",
  RECOVERY_FAILED: "Recovery failed",
  MANUAL_REVIEW: "Manual review",
};

export const stateName = (state: string) => STATES[state] ?? state;
