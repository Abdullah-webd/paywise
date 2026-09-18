import { defineRailway, github, preserve, project, service } from "railway/iac";

export default defineRailway(() => {
  const web = service("web", {
    source: github("Abdullah-webd/paywise", { checkSuites: false }),
    start: "python -m app",
    healthcheck: "/health",
    replicas: { "ams": 1 },
    env: { APP_BASE_URL: preserve(), APP_ENV: preserve(), APP_HOST: preserve(), APP_PORT: preserve(), COLLECTION_GRACE_DAYS: preserve(), ELEVENLABS_API_KEY: preserve(), ELEVENLABS_STT_MODEL: preserve(), GEMINI_API_KEY: preserve(), GEMINI_TTS_MODEL: preserve(), GEMINI_TTS_VOICE: preserve(), MONGODB_DB_NAME: preserve(), MONGODB_URI: preserve(), NOMBA_ACCOUNT_ID: preserve(), NOMBA_BASE_URL: preserve(), NOMBA_CLIENT_ID: preserve(), NOMBA_CLIENT_KEY: preserve(), NOMBA_SUB_ACCOUNT_ID: preserve(), NOMBA_VIRTUAL_ACCOUNT_TTL_HOURS: preserve(), NOMBA_WEBHOOK_SECRET: preserve(), OPENAI_API_KEY: preserve(), OPENAI_MODEL: preserve(), PORT: preserve(), TWILIO_ACCOUNT_SID: preserve(), TWILIO_AUTH_TOKEN: preserve(), TWILIO_SMS_FROM: preserve(), TWILIO_WHATSAPP_FROM: preserve() },
  });

  return project("artistic-charisma", {
    resources: [web],
  });
});
