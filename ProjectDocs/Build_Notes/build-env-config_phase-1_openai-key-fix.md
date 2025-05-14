# Task Objective
Fix the OpenAI API key configuration issue in the appointment monitoring service.

## Current State
The appointment monitor service is failing with errors because it's missing the OPENAI_API_KEY or OPENROUTER_API_KEY environment variable. The logs show consistent errors:
```
Error initializing agent for [City]: The api_key client option must be set either by passing api_key to the client or by setting the OPENAI_API_KEY environment variable
```

The service is trying to use browser-use library with OpenAI's API through OpenRouter, but the environment variables are not properly configured.

## Future State
The appointment monitor service will run successfully with proper API key configuration, enabling the agent to extract appointment data from the Schengen visa appointment website.

## Implementation Plan
1. Analyze the code to identify which API key is needed
   - [x] Review appointment_monitor.py initialization code
   - [x] Confirm the service is using OpenRouter API as a proxy for OpenAI

2. Update environment configuration
   - [x] Add OPENROUTER_API_KEY to env.example file
   - [x] Add OPENROUTER_API_KEY to env.coolify file
   - [x] Create documentation for key acquisition

3. Test the fix
   - [ ] Update the service with new environment variables
   - [ ] Verify logs show successful agent initialization

## Updates
[2025-05-14] Added OPENROUTER_API_KEY to both env.example and env.coolify files. The service now needs to be restarted with the actual API key value set.
[2025-05-14] Updated README.md with instructions on obtaining an OpenRouter API key and added troubleshooting guidance for API-related issues. 