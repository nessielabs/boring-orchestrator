import express from "express";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import routes from "./routes.js";
import { handleWebhook } from "./webhook.js";
import { startScheduler } from "./scheduler.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = Number.parseInt(process.env.PORT ?? "44066", 10);

if (!Number.isFinite(PORT) || PORT <= 0) {
  throw new Error(`Invalid PORT: ${process.env.PORT}`);
}

process.on("uncaughtException", (err) => {
  console.error(`[server] UNCAUGHT EXCEPTION:`, err.message, err.stack);
});
process.on("unhandledRejection", (reason) => {
  console.error(`[server] UNHANDLED REJECTION:`, reason);
});

app.use(express.json({ limit: "10mb" }));
app.use(express.static(join(__dirname, "public")));

app.use(routes);

// webhook endpoint: /webhook or /webhook/:path
app.post("/webhook", handleWebhook);
app.post("/webhook/:path", handleWebhook);

// SPA fallback
app.get("*", (_req, res) => {
  res.sendFile(join(__dirname, "public", "index.html"));
});

app.listen(PORT, () => {
  console.log(`[boring-orchestrator] Running at http://localhost:${PORT}`);
  startScheduler();
});
