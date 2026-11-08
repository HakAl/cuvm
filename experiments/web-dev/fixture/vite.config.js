// Loopback only: the run's D1 probe fails the run if the server listens anywhere else.
export default {
  server: { host: "127.0.0.1", port: 5173, strictPort: true },
};
