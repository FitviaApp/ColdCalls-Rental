import { Container } from "@cloudflare/containers";

export interface Env {
  COLDCALLS: DurableObjectNamespace<ColdCallsContainer>;
  BASE_URL?: string;
}

export class ColdCallsContainer extends Container {
  defaultPort = 8000;
  requiredPorts = [8000];
  sleepAfter = "12h";
  enableInternet = true;
  pingEndpoint = "/health";
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const container = env.COLDCALLS.getByName("singleton");
    const startOptions = env.BASE_URL
      ? { envVars: { BASE_URL: env.BASE_URL } }
      : undefined;

    if (startOptions) {
      await container.startAndWaitForPorts({ startOptions });
    } else {
      await container.startAndWaitForPorts();
    }

    return container.fetch(request);
  },
};
