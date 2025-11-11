# Security Policy

## 🔒 Reporting Security Vulnerabilities

**We take security seriously.** If you discover a security vulnerability, please report it responsibly.

### ⚠️ DO NOT

- ❌ Open a public GitHub issue
- ❌ Discuss the vulnerability publicly before it's fixed
- ❌ Exploit the vulnerability

### ✅ DO

1. **Email us directly**: **hi@sylphx.com**
2. **Include detailed information**:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if you have one)
3. **Wait for our response** - We aim to respond within 48 hours

---

## 🛡️ Security Response Process

### Timeline

1. **Report received** - We acknowledge within 48 hours
2. **Investigation** - We assess severity and impact (1-7 days)
3. **Fix development** - We develop and test a fix
4. **Coordinated disclosure** - We release the fix and publish security advisory
5. **Credit** - We credit the reporter (if desired)

### Communication

- We'll keep you informed throughout the process
- We'll coordinate disclosure timing with you
- We'll credit you in the security advisory (unless you prefer anonymity)

---

## 🎯 Scope

### In Scope

✅ **All SylphxAI repositories**
- MCP servers (pdf-reader-mcp, filesystem-mcp, rag-server-mcp)
- Libraries (craft, zen, silk)
- Tools and utilities
- Documentation sites

✅ **Security Issues**
- Authentication/authorization bypasses
- Code injection vulnerabilities
- Path traversal attacks
- Denial of service (DoS)
- Information disclosure
- Cryptographic weaknesses
- Dependency vulnerabilities

### Out of Scope

❌ **Not Considered Security Issues**
- Issues requiring physical access to user's machine
- Social engineering attacks
- Attacks requiring user to install malicious software
- Issues in third-party dependencies (report to the dependency maintainers)
- Theoretical vulnerabilities without proof of concept

---

## 🔐 Security Best Practices

### For Contributors

When contributing code:

1. **Never commit secrets**
   ```bash
   # ❌ Don't do this
   API_KEY=sk-1234567890abcdef

   # ✅ Do this
   API_KEY=${API_KEY}  # Read from environment
   ```

2. **Validate all inputs**
   ```typescript
   // ✅ Good
   function processFile(path: string) {
     if (!isValidPath(path)) {
       throw new Error('Invalid path');
     }
     // ...
   }
   ```

3. **Handle errors securely**
   ```typescript
   // ❌ Don't expose internals
   catch (error) {
     throw new Error(error.stack);
   }

   // ✅ Safe error messages
   catch (error) {
     throw new Error('Failed to process file');
   }
   ```

4. **Use secure dependencies**
   ```bash
   # Check for vulnerabilities
   npm audit

   # Fix vulnerabilities
   npm audit fix
   ```

### For Users

When using our tools:

1. **Keep packages updated**
   ```bash
   npm update
   ```

2. **Review permissions** - Especially for MCP servers
3. **Use environment variables** - Never hardcode secrets
4. **Enable security features** - Use sandboxing when available

---

## 📋 Supported Versions

We provide security updates for:

| Package | Supported Versions |
|---------|-------------------|
| **pdf-reader-mcp** | Latest release only |
| **filesystem-mcp** | Latest release only |
| **rag-server-mcp** | Latest release only |
| **craft** | Latest major version |
| **zen** | Latest major version |
| **silk** | Latest release only |

**Recommendation**: Always use the latest version.

---

## 🔍 Security Features

### PDF Reader MCP
- ✅ Sandboxed PDF processing
- ✅ Input validation for file paths
- ✅ Resource limits (file size, processing time)
- ✅ No external network access during processing

### Filesystem MCP
- ✅ Root directory confinement
- ✅ Path traversal protection
- ✅ Permission controls
- ✅ No access outside allowed directories

### RAG Server MCP
- ✅ Local-only processing (no cloud)
- ✅ ChromaDB data isolation
- ✅ No external API calls with user data

---

## 🚨 Known Security Considerations

### MCP Servers

**Important**: MCP servers run with your local user permissions.

⚠️ **Be cautious when**:
- Running servers from untrusted sources
- Granting filesystem access
- Processing untrusted files
- Connecting to remote MCP servers

✅ **Best Practices**:
```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@sylphx/filesystem-mcp"],
      "env": {
        "ALLOWED_DIRECTORIES": "/safe/path/only"
      }
    }
  }
}
```

---

## 📚 Security Resources

### Documentation
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [Node.js Security Best Practices](https://nodejs.org/en/docs/guides/security/)
- [MCP Security Guidelines](https://modelcontextprotocol.io/docs/security)

### Tools
- [npm audit](https://docs.npmjs.com/cli/v8/commands/npm-audit)
- [Snyk](https://snyk.io/)
- [OSSF Scorecard](https://github.com/ossf/scorecard)

---

## 🏆 Security Hall of Fame

We recognize and thank security researchers who responsibly disclose vulnerabilities:

_(No reports yet)_

---

## 📞 Contact

**Security Team**: hi@sylphx.com

For non-security issues:
- **GitHub Issues**: Use repository issue tracker
- **General Questions**: hi@sylphx.com
- **Twitter**: [@SylphxAI](https://x.com/SylphxAI)

---

## 📄 Policy Updates

This policy may be updated from time to time. Check back regularly for changes.

**Last Updated**: January 2025

---

<p align="center">
  <strong>Security is a shared responsibility</strong>
  <br>
  <sub>Thank you for helping keep Sylphx projects secure</sub>
  <br><br>
  <a href="https://sylphx.com">sylphx.com</a> •
  <a href="https://github.com/SylphxAI">GitHub</a> •
  <a href="mailto:hi@sylphx.com">hi@sylphx.com</a>
</p>
