## 🚨 Conventional Commits Setup (Local Enforcement)

We enforce **Conventional Commits** across this repository to ensure clean, structured commit messages and automatic changelog generation.

All commits must follow this format:
> `<type>(<optional scope>): <description>`  
> *Examples:* `feat: add user authentication`, `fix: resolve payload validation error`

---

### 🛠️ One-Time Local Setup

To ensure your local commit messages comply before pushing to GitHub, run the following commands in your project root:

1. **Install dependencies:**
   ```bash
   npm install