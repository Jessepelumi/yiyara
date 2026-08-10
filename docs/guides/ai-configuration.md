# AI Configuration Guide

This guide explains how to configure AI providers for the Yiyara project.

## Supported AI Providers

Yiyara currently supports:

- Google Gemini (primary)
- OpenAI ChatGPT (alternative)

## Google Gemini Setup

### 1. Obtain API Key

1. Visit [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Sign in with your Google account
3. Create a new API key
4. Copy the API key

### 2. Configure Environment

Add to your `.env` file:

```env
GEMINI_API_KEY=your-gemini-api-key-here
```

### 3. Verify Configuration

The backend will automatically use Gemini for:

- Goal decomposition
- Task generation
- SMART goal creation

## OpenAI ChatGPT Setup (Optional)

### 1. Obtain API Key

1. Visit [OpenAI Platform](https://platform.openai.com/api-keys)
2. Sign in or create an account
3. Generate a new API key
4. Copy the API key

### 2. Configure Environment

Add to your `.env` file:

```env
OPENAI_API_KEY=your-openai-api-key-here
```

### 3. Switch Provider (if needed)

Currently, the system defaults to Gemini. To use ChatGPT, modify the AI engine configuration in `backend/workflow/ai_engine.py`.

## Testing AI Features

### Goal Processing Test

1. Start the application: `make up`
2. Access the frontend at http://localhost:3000
3. Create a new goal with natural language
4. Verify that AI processes it into SMART goals and tasks

### API Test

Test the AI processing endpoint:

```bash
curl -X POST http://localhost:8000/api/goals/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-jwt-token" \
  -d '{"title": "Learn Python", "description": "I want to become proficient in Python programming"}'
```

## Troubleshooting

### Common Issues

1. **Invalid API Key**: Check that the key is correct and has proper permissions
2. **Rate Limits**: Monitor API usage in your provider dashboard
3. **Network Issues**: Ensure the backend can reach the AI provider APIs
4. **Model Availability**: Verify the requested model is available in your region

### Error Messages

- `API key not found`: Check `.env` file and restart services
- `Quota exceeded`: Upgrade your API plan or wait for reset
- `Model not available`: Try a different model or region

## Best Practices

- Keep API keys secure and never commit to version control
- Monitor API usage to avoid unexpected costs
- Use environment-specific keys (dev/staging/prod)
- Implement rate limiting in your application
- Cache AI responses where appropriate to reduce API calls

## Advanced Configuration

For custom AI behavior, modify:

- `backend/ai/providers/gemini_provider.py` - Gemini integration
- `backend/ai/providers/chatgpt_provider.py` - ChatGPT integration
- `backend/workflow/ai_engine.py` - Main AI orchestration logic</content>
  <parameter name="filePath">/Users/jeolad/Documents/yiyara/docs/guides/ai-configuration.md
