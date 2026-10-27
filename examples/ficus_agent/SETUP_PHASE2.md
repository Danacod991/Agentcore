# Phase 2 setup: Cognito sign-in and AgentCore Memory

Do these in the AWS console in **us-east-2**. When you're done, send back the
three values marked **Send back**. Phase 2 code will use them.

## Part 1: Amazon Cognito (who is asking)

### 1. Create the user pool
1. Open **Amazon Cognito**, choose **User pools**, then **Create user pool**.
2. **Application type:** *Traditional web application*. **Name:** `ficus-agent-client`.
3. **Sign-in identifiers:** *Email*. Leave self-registration **off**.
4. Choose **Create user directory**.
5. **Send back:** the **User pool ID** (looks like `us-east-2_AbC123xyz`).

### 2. Allow sign-in for testing
1. In the pool, open **App clients**, then `ficus-agent-client`, then **Edit**.
2. Under **Authentication flows**, turn on **ALLOW_USER_PASSWORD_AUTH**. Save.
3. **Send back:** the **Client ID**. Keep the **Client secret** private.

### 3. Add a test user and groups
1. Go to **Users**, choose **Create user**, and enter your email and a permanent
   password. Clear the **Send an invitation** box.
2. Optional: go to **Groups** and create groups such as `finance` and `hr`. They're
   not used in the pilot, but they're useful later.

### 4. Get a token (to test from your laptop)
```bash
SECRET_HASH=$(printf "%s" "<email><client-id>" | openssl dgst -sha256 -hmac "<client-secret>" -binary | base64)
aws cognito-idp initiate-auth --region us-east-2 \
  --auth-flow USER_PASSWORD_AUTH --client-id <client-id> \
  --auth-parameters USERNAME=<email>,PASSWORD='<password>',SECRET_HASH=$SECRET_HASH \
  --query AuthenticationResult.AccessToken --output text
```

### 5. Values for the runtime's JWT inbound auth (applied in Phase 2)
- **Discovery URL:**
  `https://cognito-idp.us-east-2.amazonaws.com/<user-pool-id>/.well-known/openid-configuration`
- **Allowed clients:** `<client-id>`

Keep your current IAM runtime for console testing. Phase 2 uses a second runtime
from the same zip with JWT auth.

> **Echo note:** the device sends one Bearer token that is set per device in
> Ficus Device Management. On the Echo, "who is asking" is the device or room,
> not a specific person. Per-person identity applies when people use the agent
> from a laptop or app. Ask your Alexa Enterprise Solution Architect how device
> tokens are refreshed. Cognito access tokens expire after 1 day at most.

## Part 2: AgentCore Memory (follow-ups and preferences)

1. Open **Amazon Bedrock AgentCore** and go to **Memory**, then **Create memory**.
2. **Name:** `ficus_agent_memory`. **Short-term memory expiration:** 30 days.
3. Under **Long-term memory strategies**, turn on:
   - **User preferences.** It learns things like "keep answers brief" or "I manage
     the Denver office".
   - **Semantic (facts).** It remembers facts users state.
   - **Summarization.** It keeps a running summary of each conversation.
4. Choose **Create memory** and wait until the status is **Active**.
5. **Send back:** the **Memory ID**.
6. Add these permissions to the runtime role
   (`AmazonBedrockAgentCoreRuntimeDefaultServiceRole-...`):
   ```json
   {
     "Effect": "Allow",
     "Action": [
       "bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents",
       "bedrock-agentcore:GetLastKTurns", "bedrock-agentcore:RetrieveMemoryRecords",
       "bedrock-agentcore:ListMemoryRecords"
     ],
     "Resource": "arn:aws:bedrock-agentcore:us-east-2:628654596877:memory/<memory-id>"
   }
   ```

## Send back
| Value | Example |
|---|---|
| User pool ID | `us-east-2_AbC123xyz` |
| App client ID | `4h3k...` |
| Memory ID | `ficus_agent_memory-AbCdEf1234` |

Never paste the client secret or passwords in chat.
