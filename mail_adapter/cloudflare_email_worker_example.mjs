import PostalMime from "postal-mime";

export default {
  async email(message, env) {
    const rawBytes = await new Response(message.raw).arrayBuffer();
    const parser = new PostalMime();
    const parsed = await parser.parse(rawBytes);

    const headers = {};
    for (const [key, value] of message.headers) {
      headers[key] = value;
    }

    const payload = {
      mailbox: message.to,
      message_id: headers["message-id"] || `${Date.now()}-${message.from}-${message.to}`,
      from_addr: message.from,
      subject: parsed.subject || headers["subject"] || "",
      text_body: parsed.text || "",
      html_body: parsed.html || "",
      raw_content: new TextDecoder().decode(rawBytes),
      headers,
      received_at: new Date().toISOString(),
      provider: "cloudflare-email-routing",
    };

    const response = await fetch(env.INBOUND_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Inbound-Token": env.INBOUND_TOKEN,
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new Error(`Inbound delivery failed: ${response.status} ${await response.text()}`);
    }
  },
};
