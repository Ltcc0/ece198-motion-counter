export class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function json(value, status = 200, headers = {}) {
  return Response.json(value, {status, headers: {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...headers}});
}
