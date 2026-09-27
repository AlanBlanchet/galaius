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
      : "Session blocked until account credit state is confirmed; visual API fallback disabled";
  }
  if (!confirmed) {
    return billing === "api_allowed"
      ? "Session blocked; metered API fallback permitted"
      : "Session blocked until account credit state is confirmed";
  }
  return billing === "api_allowed"
    ? "Session account impact unknown; metered API fallback permitted"
    : "Session active; account impact unknown";
}
