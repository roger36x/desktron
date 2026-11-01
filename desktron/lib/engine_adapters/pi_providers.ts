/** Desk-owned Pi providers. User/project extensions remain disabled by the host. */
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import registerProviderTelemetry from './pi_provider_telemetry.ts';

export default async function registerDeskProviders(pi: ExtensionAPI) {
  registerProviderTelemetry(pi);
}
