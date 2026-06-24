**AGENTS.md is a simple, open format for guiding AI agents that works with your repository.**

> Edit the following template to provide users with a better experience when working with coding agents (VS Code, GitHub Copilot, Cursor, Codex, Gemini CLI, etc.).

Provide the agent with information on which documentation and/or OpenAPI spec document to use.  
For example, you can paste a direct link to the latest OpenAPI spec. 
Direct link for Cisco Secure Access Authorization API:
https://pubhub.devnetcloud.com/media/cloud-security-apis-in-eft/docs/secure-access/reference/auth/cisco_secure_access_token_authorization_api_2_0_0.yaml 

Add information about the MCP servers needed for the project.

You can add a link to the Cisco DevNet sandbox needed to work with the project. 
Choose the direct link from the list here: https://devnetsandbox.cisco.com/DevNet

Additionally, you can provide information about the version and link to the SDK, Infrastructure as Code provider/module, etc., which is recommended for use by coding Agents.

Example of AGENTS.md file at Cisco DevNet GitHub org: [https://github.com/CiscoDevNet/python_code_samples_network/blob/master/AGENTS.md](https://github.com/CiscoDevNet/python_code_samples_network/blob/master/AGENTS.md)

Example of AGENTS.md file at The Apache Software Foundation org: [https://github.com/apache/airflow/blob/main/AGENTS.md](https://github.com/apache/airflow/blob/main/AGENTS.md)

**How to test AGENTS.md?**
* You can test it with your favorite coding agents (VS Code, GitHub Copilot, Cursor, Codex, Gemini CLI, etc.).
* Clone the repository containing the updated AGENTS.md and request guidance, such as: `How do I run this project with the [specified parameters]?`

# Template:

## Dev environment tips

- **Python version**: Use Python 3.9+.
- **Virtual env (recommended)**:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install -U pip
  ```

### Quick run examples


## Testing instructions

- **MCP server links**

- **Test the code with the Cisco DevNet sandbox**
  
  Visit https://devnetsandbox.cisco.com/DevNet to book a related sandbox.
  
- **Latest Cisco API documentation**:
  
  https://developer.cisco.com/docs/

## PR instructions

- **Security**: Do not commit real credentials or tokens. Use placeholders and document required env vars or files.

## Contribution conventions

- **Backward compatibility**: Do not change existing sample behavior unless clearly improving or fixing a bug; document changes.
