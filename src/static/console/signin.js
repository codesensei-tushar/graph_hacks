// /signin: one button to Slack. The sign-in callback comes back here with ?error=<code> when it
// refuses; the page shows a fixed sentence per code and never echoes text from the address.
(() => {
  "use strict";

  const MESSAGES = {
    guest: "Guests and people from other organisations in Slack Connect channels cannot sign in. Ask someone who is a full member of the workspace.",
    denied: "Sign-in was cancelled in Slack. Try again when you are ready.",
    expired: "That sign-in link expired or was already used. Start again.",
    failed: "Slack sign-in did not complete. Try again in a moment.",
    signed_out: "You are signed out.",
    session: "Your session ended. Sign in again to continue.",
  };

  const params = new URLSearchParams(location.search);
  const code = params.get("error") || (params.has("signed_out") ? "signed_out" : params.has("expired") ? "session" : "");
  if (!code) return;
  const box = document.getElementById("signin-message");
  box.textContent = MESSAGES[code] || MESSAGES.failed;
  box.classList.toggle("error", code !== "signed_out" && code !== "session");
  box.hidden = false;
})();
