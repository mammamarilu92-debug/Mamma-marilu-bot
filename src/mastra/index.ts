import { Mastra } from "@mastra/core";
import * as fs from "fs";
import * as path from "path";
import { execSync } from "child_process";
import { MastraError } from "@mastra/core/error";
import { PinoLogger } from "@mastra/loggers";
import { LogLevel, MastraLogger } from "@mastra/core/logger";
import pino from "pino";
import { MCPServer } from "@mastra/mcp";
import { NonRetriableError } from "inngest";
import { z } from "zod";

import { sharedPostgresStorage } from "./storage";
import { inngest, inngestServe } from "./inngest";
import { exampleWorkflow } from "./workflows/exampleWorkflow"; // Replace with your own workflow
import { exampleAgent } from "./agents/exampleAgent"; // Replace with your own agent
import { startBotManager } from "../botManager";

let botManagerStarted = false;

class ProductionPinoLogger extends MastraLogger {
  protected logger: pino.Logger;

  constructor(
    options: {
      name?: string;
      level?: LogLevel;
    } = {},
  ) {
    super(options);

    this.logger = pino({
      name: options.name || "app",
      level: options.level || LogLevel.INFO,
      base: {},
      formatters: {
        level: (label: string, _number: number) => ({
          level: label,
        }),
      },
      timestamp: () => `,"time":"${new Date(Date.now()).toISOString()}"`,
    });
  }

  debug(message: string, args: Record<string, any> = {}): void {
    this.logger.debug(args, message);
  }

  info(message: string, args: Record<string, any> = {}): void {
    this.logger.info(args, message);
  }

  warn(message: string, args: Record<string, any> = {}): void {
    this.logger.warn(args, message);
  }

  error(message: string, args: Record<string, any> = {}): void {
    this.logger.error(args, message);
  }
}

export const mastra = new Mastra({
  storage: sharedPostgresStorage,
  // Register your workflows here
  workflows: {},
  // Register your agents here
  agents: {},
  mcpServers: {
    allTools: new MCPServer({
      name: "allTools",
      version: "1.0.0",
      tools: {},
    }),
  },
  bundler: {
    // A few dependencies are not properly picked up by
    // the bundler if they are not added directly to the
    // entrypoint.
    externals: [
      "@slack/web-api",
      "inngest",
      "inngest/hono",
      "hono",
      "hono/streaming",
    ],
    // sourcemaps are good for debugging.
    sourcemap: true,
  },
  server: {
    host: "0.0.0.0",
    port: 5000,
    middleware: [
      async (c, next) => {
        const mastra = c.get("mastra");
        const logger = mastra?.getLogger();
        
        
        logger?.debug("[Request]", { method: c.req.method, url: c.req.url });
        try {
          await next();
        } catch (error) {
          logger?.error("[Response]", {
            method: c.req.method,
            url: c.req.url,
            error,
          });
          if (error instanceof MastraError) {
            if (error.id === "AGENT_MEMORY_MISSING_RESOURCE_ID") {
              // This is typically a non-retirable error. It means that the request was not
              // setup correctly to pass in the necessary parameters.
              throw new NonRetriableError(error.message, { cause: error });
            }
          } else if (error instanceof z.ZodError) {
            // Validation errors are never retriable.
            throw new NonRetriableError(error.message, { cause: error });
          }

          throw error;
        }
      },
    ],
    apiRoutes: [
      // ======================================================================
      // Inngest Integration Endpoint
      // ======================================================================
      // This API route is used to register the Mastra workflow (inngest function) on the inngest server
      {
        path: "/posttap-proxy",
        method: "POST",
        createHandler: async ({ mastra }) => {
          return async (c: any) => {
            const logger = mastra?.getLogger();
            try {
              const body = await c.req.json();
              const amazonUrl = body.url;
              const name = body.name || "link";
              logger?.info("🔗 [PostTap Proxy] Richiesta shortlink", { url: amazonUrl });

              // Usa Python httpx (Node.js fetch non invia Cookie header correttamente)
              const scriptPath = path.join(process.cwd(), "telegram_bot", "posttap_proxy.py");
              const safeUrl = amazonUrl.replace(/"/g, '\\"');
              const safeName = name.replace(/"/g, '\\"');
              let output: string;
              try {
                output = execSync(`python3 "${scriptPath}" "${safeUrl}" "${safeName}"`, {
                  timeout: 20000,
                  encoding: "utf8",
                });
              } catch (execErr: any) {
                output = execErr.stdout || execErr.stderr || "{}";
              }
              logger?.info("📡 [PostTap Proxy] Python output", { output: output.trim() });

              let result: any = {};
              try { result = JSON.parse(output.trim()); } catch (_) {}

              if (result.shortlink) {
                logger?.info("✅ [PostTap Proxy] Shortlink creato", { shortlink: result.shortlink });
                return c.json({ shortlink: result.shortlink });
              }
              logger?.warn("⚠️ [PostTap Proxy] Nessun shortlink", { result });
              return c.json({ error: result.error || "no_shortlink", shortlink: null }, 502);
            } catch (e: any) {
              logger?.error("❌ [PostTap Proxy] Eccezione", { error: e?.message });
              return c.json({ error: e?.message, shortlink: null }, 500);
            }
          };
        },
      },
      {
        path: "/api/inngest",
        method: "ALL",
        createHandler: async ({ mastra }) => inngestServe({ mastra, inngest }),
        // The inngestServe function integrates Mastra workflows with Inngest by:
        // 1. Creating Inngest functions for each workflow with unique IDs (workflow.${workflowId})
        // 2. Setting up event handlers that:
        //    - Generate unique run IDs for each workflow execution
        //    - Create an InngestExecutionEngine to manage step execution
        //    - Handle workflow state persistence and real-time updates
        // 3. Establishing a publish-subscribe system for real-time monitoring
        //    through the workflow:${workflowId}:${runId} channel
      },

      // ======================================================================
      // Connector Webhook Triggers
      // ======================================================================
      // Register your connector webhook handlers here using the spread operator.
      // Each connector trigger should be defined in src/triggers/{connectorName}Triggers.ts
      //
      // PATTERN FOR ADDING A NEW CONNECTOR TRIGGER:
      //
      // 1. Create a trigger file: src/triggers/{connectorName}Triggers.ts
      //    (See src/triggers/exampleConnectorTrigger.ts for a complete example)
      //
      // 2. Create a workflow: src/mastra/workflows/{connectorName}Workflow.ts
      //    (See src/mastra/workflows/linearIssueWorkflow.ts for an example)
      //
      // 3. Import both in this file:
      //    ```typescript
      //    import { register{ConnectorName}Trigger } from "../triggers/{connectorName}Triggers";
      //    import { {connectorName}Workflow } from "./workflows/{connectorName}Workflow";
      //    ```
      //
      // 4. Register the trigger in the apiRoutes array below:
      //    ```typescript
      //    ...register{ConnectorName}Trigger({
      //      triggerType: "{connector}/{event.type}",
      //      handler: async (mastra, triggerInfo) => {
      //        const logger = mastra.getLogger();
      //        logger?.info("🎯 [{Connector} Trigger] Processing {event}", {
      //          // Log relevant fields from triggerInfo.params
      //        });
      //
      //        // Create a unique thread ID for this event
      //        const threadId = `{connector}-{event}-${triggerInfo.params.someUniqueId}`;
      //
      //        // Start the workflow
      //        const run = await {connectorName}Workflow.createRunAsync();
      //        return await run.start({
      //          inputData: {
      //            threadId,
      //            ...triggerInfo.params,
      //          },
      //        });
      //      }
      //    })
      //    ```
      //
      // ======================================================================
      // EXAMPLE: Linear Issue Creation Webhook
      // ======================================================================
      // Uncomment to enable Linear webhook integration:
      //
      // ...registerLinearTrigger({
      //   triggerType: "linear/issue.created",
      //   handler: async (mastra, triggerInfo) => {
      //     // Extract what you need from the full payload
      //     const data = triggerInfo.payload?.data || {};
      //     const title = data.title || "Untitled";
      //
      //     // Start your workflow
      //     const run = await exampleWorkflow.createRunAsync();
      //     return await run.start({
      //       inputData: {
      //         message: `Linear Issue: ${title}`,
      //         includeAnalysis: true,
      //       }
      //     });
      //   }
      // }),
      //
      // To activate:
      // 1. Uncomment the code above
      // 2. Import at the top: import { registerLinearTrigger } from "../triggers/exampleConnectorTrigger";
      //
      // ======================================================================

    ],
  },
  logger:
    process.env.NODE_ENV === "production"
      ? new ProductionPinoLogger({
          name: "Mastra",
          level: "info",
        })
      : new PinoLogger({
          name: "Mastra",
          level: "info",
        }),
});

/*  Sanity check 1: Throw an error if there are more than 1 workflows.  */
// !!!!!! Do not remove this check. !!!!!!
if (Object.keys(mastra.getWorkflows()).length > 1) {
  throw new Error(
    "More than 1 workflows found. Currently, more than 1 workflows are not supported in the UI, since doing so will cause app state to be inconsistent.",
  );
}

/*  Sanity check 2: Throw an error if there are more than 1 agents.  */
// !!!!!! Do not remove this check. !!!!!!
if (Object.keys(mastra.getAgents()).length > 1) {
  throw new Error(
    "More than 1 agents found. Currently, more than 1 agents are not supported in the UI, since doing so will cause app state to be inconsistent.",
  );
}

// BotManager disabilitato su Replit — il bot gira su Render
// if (!botManagerStarted) {
//   botManagerStarted = true;
//   const logger = mastra.getLogger();
//   logger?.info("🤖 [Bootstrap] Starting Telegram bot manager...");
//   startBotManager(logger);
// }

// Cancella subito il webhook Telegram — Inngest lo ri-registra ogni volta che parte
// Il bot su Render usa polling, non webhook
const _token = process.env.TELEGRAM_BOT_TOKEN;
if (_token) {
  fetch(`https://api.telegram.org/bot${_token}/deleteWebhook`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ drop_pending_updates: false }),
  })
    .then(() => console.log("✅ [Bootstrap] Webhook Telegram cancellato"))
    .catch((e) => console.warn("⚠️ [Bootstrap] Errore cancellazione webhook:", e));
  
  // Ripeti ogni 15 secondi per battere Inngest
  setInterval(() => {
    fetch(`https://api.telegram.org/bot${_token}/getWebhookInfo`)
      .then(r => r.json())
      .then((data: any) => {
        if (data?.result?.url) {
          fetch(`https://api.telegram.org/bot${_token}/deleteWebhook`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ drop_pending_updates: false }),
          }).then(() => console.log("🛡️ [Watchdog Mastra] Webhook cancellato"));
        }
      })
      .catch(() => {});
  }, 15000);
}
