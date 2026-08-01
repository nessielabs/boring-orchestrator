export interface OpenAIUsage {
  input_tokens?: number;
  cached_input_tokens?: number;
  cache_write_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
}

interface ModelPricing {
  input: number;
  cached_input: number;
  output: number;
  cache_write_multiplier?: number;
  long_context_threshold?: number;
}

// OpenAI pricing per million tokens (https://developers.openai.com/api/docs/pricing).
// Models with long_context_threshold apply 2x input and 1.5x output for the full
// session when input_tokens exceeds the threshold.
const OPENAI_PRICING: Record<string, ModelPricing> = {
  // GPT-5.x (5.6, 5.5, and 5.4 have >272K long-context multiplier)
  "gpt-5.6-sol": { input: 5.00, cached_input: 0.50, output: 30.00, cache_write_multiplier: 1.25, long_context_threshold: 272_000 },
  "gpt-5.6":     { input: 5.00, cached_input: 0.50, output: 30.00, cache_write_multiplier: 1.25, long_context_threshold: 272_000 },
  "gpt-5.5":      { input: 5.00,  cached_input: 0.50,  output: 30.00, long_context_threshold: 272_000 },
  "gpt-5.4":      { input: 2.50,  cached_input: 0.25,  output: 15.00, long_context_threshold: 272_000 },
  "gpt-5.4-mini": { input: 0.75,  cached_input: 0.075, output: 4.50 },
  "gpt-5.4-nano": { input: 0.20,  cached_input: 0.02,  output: 1.25 },
  "gpt-5.3-codex": { input: 1.75, cached_input: 0.175, output: 14.00 },
  // GPT-4.1
  "gpt-4.1":      { input: 2.00,  cached_input: 0.50,  output: 8.00 },
  "gpt-4.1-mini": { input: 0.40,  cached_input: 0.10,  output: 1.60 },
  "gpt-4.1-nano": { input: 0.10,  cached_input: 0.025, output: 0.40 },
  // GPT-4o
  "gpt-4o":       { input: 2.50,  cached_input: 1.25,  output: 10.00 },
  "gpt-4o-mini":  { input: 0.15,  cached_input: 0.075, output: 0.60 },
  // o-series reasoning
  "o4-mini":      { input: 1.10,  cached_input: 0.275, output: 4.40 },
  "o3":           { input: 2.00,  cached_input: 0.50,  output: 8.00 },
  "o3-mini":      { input: 1.10,  cached_input: 0.55,  output: 4.40 },
};

export function computeOpenAICost(model: string, usage: OpenAIUsage): number | null {
  const pricing = OPENAI_PRICING[model];
  if (!pricing) return null;

  const input = Math.max(usage.input_tokens ?? 0, 0);
  const cached = Math.max(Math.min(usage.cached_input_tokens ?? 0, input), 0);
  const nonCachedInput = input - cached;
  // Codex reports cache writes as a subset of non-cached input tokens.
  const cacheWrite = Math.max(Math.min(usage.cache_write_input_tokens ?? 0, nonCachedInput), 0);
  const standardInput = nonCachedInput - cacheWrite;
  const output = Math.max(usage.output_tokens ?? 0, 0);

  const longContext = pricing.long_context_threshold != null && input > pricing.long_context_threshold;
  const inputRate = longContext ? pricing.input * 2 : pricing.input;
  const cachedRate = longContext ? pricing.cached_input * 2 : pricing.cached_input;
  const outputRate = longContext ? pricing.output * 1.5 : pricing.output;
  const cacheWriteRate = inputRate * (pricing.cache_write_multiplier ?? 1);

  return (
    standardInput * inputRate +
    cacheWrite * cacheWriteRate +
    cached * cachedRate +
    output * outputRate
  ) / 1_000_000;
}
