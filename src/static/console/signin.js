// /signin: one button to Slack. The sign-in routes (src/api/v1/auth.py) come back here with
// ?error=<code> when they refuse, and logout with ?signed_out; the page shows a fixed sentence per
// code and never echoes text from the address. An unknown code reads as a calm retry.
(() => {
  "use strict";

  const GUEST = "Only full members of the workspace can sign in. Guests and people from other organisations in Slack Connect channels cannot; ask a full member instead.";
  const RETRY = "Sign-in is not available just now. Try again in a few minutes.";
  const MESSAGES = {
    guest: GUEST,
    not_a_member: GUEST,
    denied: "Sign-in was cancelled in Slack. Try again when you are ready.",
    expired: "That sign-in took too long or was already used. Start again.",
    wrong_browser: "Start sign-in again from this browser. It has to finish in the browser that started it.",
    bad_token: "Slack's answer could not be verified, so you were not signed in. Start again.",
    slack_refused: "Slack did not complete the sign-in. Start again.",
    slack_unavailable: "Slack could not be reached. Try again in a few minutes.",
    workspace_unavailable: "Your workspace could not be checked just now. Try again in a few minutes.",
    unavailable: RETRY,
    failed: RETRY,
    signed_out: "You are signed out.",
    session: "Your session ended. Sign in again to continue.",
  };
  const CALM = new Set(["signed_out", "session"]);

  const params = new URLSearchParams(location.search);
  const code = params.get("error") || (params.has("signed_out") ? "signed_out" : params.has("expired") ? "session" : "");
  if (!code) return;
  const box = document.getElementById("signin-message");
  box.textContent = Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] : RETRY;
  box.classList.toggle("error", !CALM.has(code));
  box.hidden = false;
})();
