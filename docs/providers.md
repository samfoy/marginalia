# AI Provider Setup

marginalia works with three LLM backends. You only need one. Pick the one that fits your situation.

---

## OpenAI (recommended for most users)

**Best for:** straightforward setup, best Book Index quality, widest model choice.

### Get an API key

1. Sign in at [platform.openai.com](https://platform.openai.com)
2. Go to **API keys** → **Create new secret key**
3. Copy the key (starts with `sk-`)

### Configure

Add to your shell or LaunchAgent plist:

```bash
export MARGINALIA_OPENAI_API_KEY=sk-...
export MARGINALIA_MODEL_ID=openai:gpt-4o
```

### Recommended models

| Use case | Model ID | Notes |
|---|---|---|
| Best quality | `openai:gpt-4o` | Recommended for Book Index generation |
| Faster / cheaper | `openai:gpt-4o-mini` | Good for companion calls (recap, wiki, chat) |
| Reasoning tasks | `openai:o3-mini` | Better on complex series questions |

### Fallback chain

With an OpenAI key, the auto-derived chain is:

```
openai:gpt-4o → openai:gpt-4o-mini
```

Override with `MARGINALIA_MODEL_CHAIN=openai:gpt-4o,openai:gpt-4o-mini` if you want to be explicit.

### Cost estimates

Book Index generation for a typical novel (~400 pages) uses roughly 80–150K input tokens + 4–8K output tokens with `gpt-4o`. At current pricing that's **$0.40–$1.20 per book**, one-time (the result is cached). Companion calls (recap, wiki, chat) use 1–8K tokens each.

---

## Anthropic

**Best for:** if you already have Anthropic credits, or want to avoid OpenAI.

### Get an API key

1. Sign in at [console.anthropic.com](https://console.anthropic.com)
2. Go to **API Keys** → **Create Key**
3. Copy the key (starts with `sk-ant-`)

### Configure

```bash
export MARGINALIA_ANTHROPIC_API_KEY=sk-ant-...
export MARGINALIA_MODEL_ID=anthropic:claude-opus-4-5
```

### Recommended models

| Use case | Model ID | Notes |
|---|---|---|
| Best quality | `anthropic:claude-opus-4-5` | Best for Book Index generation |
| Faster / cheaper | `anthropic:claude-haiku-3-5` | Good for companion calls |
| Balanced | `anthropic:claude-sonnet-4-5` | Middle ground |

### Fallback chain

With an Anthropic key:

```
anthropic:claude-opus-4-5 → anthropic:claude-haiku-3-5
```

### Cost

Similar to OpenAI gpt-4o. Claude Opus is on the expensive end for Book Index generation; use `claude-haiku-3-5` as your primary if cost matters and you're happy with slightly lighter entity extraction.

---

## AWS Bedrock

**Best for:** if you have AWS credentials set up and want to avoid external API keys, or if you need the bedrock-mantle GPT models.

Bedrock gives you two sub-options:

### A) Anthropic Claude via Bedrock (invoke_model)

```bash
export MARGINALIA_AWS_PROFILE=your-profile
export MARGINALIA_AWS_REGION=us-west-2
export MARGINALIA_MODEL_ID=us.anthropic.claude-sonnet-4-6
```

Common model IDs:
- `us.anthropic.claude-opus-4-5` — highest quality
- `us.anthropic.claude-sonnet-4-6` — balanced (default fallback)
- `us.anthropic.claude-haiku-3-5` — fastest

### B) OpenAI GPT via bedrock-mantle

> **⚠ Amazon-internal only.** `bedrock-mantle.us-east-2.api.aws` is an AWS-internal service that requires your account to be explicitly allowlisted. External users will get auth failures. If you're not an Amazon employee with Bedrock access, use Option A (direct OpenAI) instead.

bedrock-mantle (`bedrock-mantle.us-east-2.api.aws`) gives access to GPT models using SigV4 auth. Your AWS account must be allowlisted. Model IDs use the `openai.` prefix (without a colon — this is the legacy format that routes to bedrock-mantle):

```bash
export MARGINALIA_AWS_PROFILE=your-profile
export MARGINALIA_MODEL_ID=openai.gpt-4o  # bedrock-mantle; requires allowlisted AWS account
```

### IAM setup

Your AWS profile needs permission to call Bedrock. The minimum required actions:
```
bedrock:InvokeModel
bedrock:InvokeModelWithResponseStream
```

A typical `~/.aws/config` entry:

```ini
[profile marginalia]
region = us-west-2
role_arn = arn:aws:iam::123456789:role/YourBedrockRole
source_profile = default
```

Then set `MARGINALIA_AWS_PROFILE=marginalia`.

---

## Using multiple providers (fallback chain)

marginalia can fall back across providers. Set `MARGINALIA_MODEL_CHAIN` to a comma-separated list:

```bash
# Try OpenAI first, fall back to Anthropic, then Bedrock
export MARGINALIA_MODEL_CHAIN=openai:gpt-4o,anthropic:claude-haiku-3-5,us.anthropic.claude-sonnet-4-6
```

A per-model circuit breaker (`MARGINALIA_MODEL_COOLDOWN_S`, default 120s) skips recently-failed models and re-probes automatically — no manual intervention when a provider has an outage.

---

## Embeddings (for RAG)

RAG (position-bounded retrieval) requires an embedding model in addition to the main LLM. Options, in priority order:

| Backend | How to enable | Model | Quality |
|---|---|---|---|
| OpenAI | `MARGINALIA_OPENAI_API_KEY` set | `text-embedding-3-small` | High |
| Local | `pip install sentence-transformers` | `all-MiniLM-L6-v2` (~80MB) | Good |
| Bedrock Cohere | AWS credentials | `cohere.embed-english-v3` | High |

Auto-detection: OpenAI key present → OpenAI; sentence-transformers installed → local; else → Bedrock.

To force a specific backend:
```bash
export MARGINALIA_EMBED_BACKEND=local   # always use sentence-transformers
export MARGINALIA_EMBED_BACKEND=openai  # always use OpenAI
```

To use a different local model (e.g. higher quality at the cost of size):
```bash
export MARGINALIA_LOCAL_EMBED_MODEL=all-mpnet-base-v2  # ~420MB, higher quality
```

---

## Environment variable reference

| Variable | Description |
|---|---|
| `MARGINALIA_OPENAI_API_KEY` | OpenAI API key (also checks `OPENAI_API_KEY`) |
| `MARGINALIA_ANTHROPIC_API_KEY` | Anthropic API key (also checks `ANTHROPIC_API_KEY`) |
| `MARGINALIA_MODEL_ID` | Primary model (default: `openai:gpt-4o`) |
| `MARGINALIA_FALLBACK_MODEL_ID` | Terminal fallback (default: `us.anthropic.claude-sonnet-4-6`) |
| `MARGINALIA_MODEL_CHAIN` | Explicit chain, overrides auto-derivation |
| `MARGINALIA_MODEL_COOLDOWN_S` | Circuit breaker window in seconds (default: 120) |
| `MARGINALIA_AWS_PROFILE` | AWS profile for Bedrock |
| `MARGINALIA_AWS_REGION` | AWS region for Bedrock (default: `us-west-2`) |
| `MARGINALIA_MAX_TOKENS` | Max tokens for companion responses (default: 600) |
| `MARGINALIA_COMPANION_EFFORT` | Reasoning effort `none\|low\|medium\|high` (default: `low`) |
| `MARGINALIA_EMBED_BACKEND` | Embedding backend: `auto\|local\|openai\|bedrock` |
| `MARGINALIA_LOCAL_EMBED_MODEL` | sentence-transformers model (default: `all-MiniLM-L6-v2`) |
| `MARGINALIA_XRAY_MAX_TOKENS` | Max tokens for Book Index generation (default: 16384) |
