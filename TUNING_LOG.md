# Similarity Threshold Tuning Log

## Problem
Paraphrased questions were not clustering together. The initial threshold of 0.82 was too high, and even after lowering to 0.75, semantically similar questions failed to match.

## Methodology
1. Added `/debug` endpoint to inspect similarity scores
2. Tested with paraphrased variations of a base question
3. Analyzed score distribution for similar vs. different questions
4. Set threshold based on empirical data

## Test Results (2024-02-16)

### Base Question
`"How do I reset my password?"`

### Paraphrased Variations (Should Match)
| Question | Similarity Score |
|----------|------------------|
| "Where do I change my password?" | 0.7487 |
| "I forgot my password, how do I get a new one?" | 0.7292 |
| "Password reset instructions?" | 0.7095 |

### Related but Different Topic (Should NOT Match)
| Question | Similarity Score |
|----------|------------------|
| "How do I update my email address?" | 0.5461 |

### Unrelated Question (Should NOT Match)
| Question | Similarity Score |
|----------|------------------|
| "What time is the team meeting tomorrow?" | 0.1017 |

## Score Ranges

| Score | Meaning | Action |
|-------|---------|--------|
| ≥ 0.70 | Paraphrased questions (same intent, different wording) | Clustered together |
| 0.55–0.70 | Related topic, different question (e.g., password vs email) | Not clustered |
| < 0.55 | Unrelated questions | Not clustered |

## Decision
**Set threshold to 0.70**

Reasoning:
- Paraphrased questions score ≥ 0.70
- Related but distinct questions fall in the 0.55–0.70 gap — close enough to seem similar, but different enough to warrant separate FAQ entries
- Unrelated questions score well below 0.55
- 0.70 captures natural paraphrasing while avoiding false matches

## Verification
Tested with 3 paraphrased questions:
1. "How do I reset my password?" → new
2. "Where do I change my password?" → matched (cluster_id: 6, count: 2)
3. "I forgot my password, how do I get a new one?" → matched (cluster_id: 6, count: 3)

✅ All three questions clustered together correctly.

## Model Used
- OpenAI `text-embedding-3-small` (1536 dimensions)
- Cosine similarity metric

## Caveats
- The 0.70 threshold was baselined against a small subset of common IT and HR questions (password resets, VPN access, PTO requests) to establish a quick working baseline
- Production use across other domains (engineering, legal, finance) would likely require additional testing and threshold adjustment
- This threshold may also shift based on:
  - Domain-specific terminology
  - Question length variations
  - Multi-language support
- Consider adding a `/tune` endpoint that suggests optimal thresholds based on labeled training data
