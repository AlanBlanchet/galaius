export function presentMediaStatus(
  billing: "session_only" | "api_allowed",
  confirmed: boolean,
  backend: "auto" | "session" | "api",
  apiEnabled: boolean,
): string {
  if (backend === "api") return "Metered API transport enabled";
  if (billing === "api_allowed" && !apiEnabled) {
    return confirmed
      ? "Session account impact unknown; visual API fallback disabled"
      : "Session active; extra usage not confirmed off; visual API fallback disabled";
  }
  if (!confirmed) {
    return billing === "api_allowed"
      ? "Session active; extra usage not confirmed off; metered API fallback permitted"
      : "Session active; extra usage not confirmed off";
  }
  return billing === "api_allowed"
    ? "Session account impact unknown; metered API fallback permitted"
    : "Session active; account impact unknown";
}
