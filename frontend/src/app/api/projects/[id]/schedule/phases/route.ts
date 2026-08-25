// Compatibility endpoint: phase creation is also exposed under the explicit
// collection path used by API clients that distinguish phases from schedule
// reads. Keep validation/authentication in the canonical route.
export { POST } from "../route";
