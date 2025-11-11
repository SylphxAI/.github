# Sylphx Brand Guidelines

**Official brand guidelines for Sylphx Limited**

---

## 🏢 Company Name

### Official Name
**Full:** Sylphx Limited
**Short:** Sylphx
**Code/GitHub:** SylphxAI

### Usage
✅ **Correct:**
- "Sylphx Limited" - Legal documents
- "Sylphx" - Marketing, website, general use
- "@SylphxAI" - GitHub organization, social media handles

❌ **Incorrect:**
- SylphX, Sylph X, SylphxAI (except for GitHub org)
- sylphx, SYLPHX (except in code: @sylphx npm scope)

---

## 🎨 Logo Usage

### Logo Formats
- **Primary:** SVG (scalable, preferred)
- **Fallback:** PNG with transparent background
- **Minimum size:** 120px width for digital, 1 inch for print

### Clear Space
Maintain clear space around the logo equal to the height of the "S" in Sylphx.

```
┌─────────────────────────┐
│         SPACE           │
│   ┌─────────────┐       │
│   │   Sylphx    │  SPACE│
│   └─────────────┘       │
│         SPACE           │
└─────────────────────────┘
```

### Logo Variations
1. **Full Color** - Use on white/light backgrounds
2. **Monochrome** - Use when color isn't available
3. **White** - Use on dark backgrounds

### Don'ts ❌
- Don't stretch or distort the logo
- Don't rotate the logo
- Don't add effects (shadows, gradients)
- Don't place on busy backgrounds
- Don't use unapproved colors

---

## 🎯 Tagline

### Primary Tagline
> "Empowering developers to build the future with AI"

### Alternative Taglines
- "Building AI Agent Infrastructure"
- "Democratizing AI capabilities through open-source infrastructure"

### Usage
- Use in website hero sections
- Include in major announcements
- Use in conference materials

---

## ✍️ Typography

### Primary Font: Inter
**Usage:** UI, website, documentation

```css
font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
```

### Monospace Font: JetBrains Mono
**Usage:** Code, technical documentation

```css
font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', monospace;
```

### Font Weights
- **Regular (400)** - Body text
- **Medium (500)** - Subheadings
- **Semibold (600)** - Emphasis
- **Bold (700)** - Headings

---

## 🎨 Colors

See [colors.md](colors.md) for complete color palette.

### Quick Reference
- **Brand Blue:** `#4A90E2`
- **Brand Dark:** `#1A1A2E`
- **Success:** `#27AE60`
- **Error:** `#E74C3C`

---

## 🏷️ Badges

### Style
Use **flat-square** style for consistency:

```markdown
![npm version](https://img.shields.io/npm/v/@sylphx/package?style=flat-square)
```

### Standard Badges
- npm version
- License (MIT)
- CI Status
- Coverage
- Downloads

---

## 📱 Social Media

### Profile Images
- Use primary logo on white background
- Minimum 400x400px
- Square format (1:1 aspect ratio)

### Cover Images
- Use brand colors (#4A90E2 background)
- Include tagline
- 1500x500px (Twitter), 1200x628px (Facebook)

### Handles
- **GitHub:** @SylphxAI
- **Twitter/X:** @SylphxAI
- **npm:** @sylphx (scope)
- **Medium:** @shtse8

---

## 📧 Email Signatures

### Standard Format
```
[Name]
[Title]
Sylphx Limited

📧 hi@sylphx.com
🌐 sylphx.com
🐙 github.com/SylphxAI
```

---

## 📄 Documentation

### README Structure
Follow template at [templates/README-template.md](../templates/README-template.md)

### Footer Format
```markdown
---

<p align="center">
  <strong>Empowering developers to build the future with AI</strong>
  <br>
  <sub>© 2025 Sylphx Limited</sub>
  <br><br>
  <a href="https://sylphx.com">sylphx.com</a> •
  <a href="https://x.com/SylphxAI">@SylphxAI</a> •
  <a href="mailto:hi@sylphx.com">hi@sylphx.com</a>
</p>
```

---

## 📦 Package Naming

### npm Packages
```
@sylphx/<package-name>
```
**Examples:** @sylphx/pdf-reader-mcp, @sylphx/craft

### Docker Images
```
sylphx/<image-name>
```
**Examples:** sylphx/filesystem-mcp

### GitHub Repositories
```
github.com/SylphxAI/<repo-name>
```

---

## 🎤 Voice & Tone

### Brand Voice
- **Professional** but approachable
- **Technical** but accessible
- **Confident** but not arrogant
- **Helpful** and supportive

### Writing Style
✅ **Do:**
- Use active voice
- Be concise and clear
- Use technical terms appropriately
- Focus on benefits, not just features
- Include specific metrics and data

❌ **Don't:**
- Use jargon unnecessarily
- Make unsubstantiated claims
- Be overly formal or stuffy
- Use marketing buzzwords excessively

### Example Comparisons

**❌ Bad:**
> "Our revolutionary, cutting-edge, enterprise-grade solution leverages bleeding-edge AI to synergize your workflow paradigm."

**✅ Good:**
> "PDF Reader MCP processes 50-page PDFs in seconds with 5-10x faster performance than sequential processing."

---

## 📊 Data & Metrics

### Formatting
- Use specific numbers: "94% test coverage" not "high coverage"
- Include comparisons: "5-10x faster" not just "fast"
- Show evidence: Link to benchmarks, tests, examples

### Achievement Format
```markdown
- 🤖 **300+ GitHub stars** on flagship projects
- 📦 **8,000+ npm downloads** across packages
- ✅ **94%+ test coverage** on production systems
```

---

## 🌟 Values & Mission

### Core Values
```
🎯 Production-Ready Quality → Ship code you're proud of
🌍 Open Source First → Community over competition
⚡ Performance Matters → Every millisecond counts
🔐 Security by Design → Trust through transparency
💡 Developer Experience → Tools that developers love
🤝 Knowledge Sharing → Learn together, grow together
```

### Mission Statement
> "Democratizing AI capabilities through open-source infrastructure."

---

## ⚖️ Legal

### Copyright Notice
```
© 2025 Sylphx Limited. All rights reserved.
```

### License
All open-source projects use **MIT License**.

### Trademark
"Sylphx" is a trademark of Sylphx Limited.

---

## 📞 Contact Information

### Primary Contact
- **Email:** hi@sylphx.com
- **Website:** https://sylphx.com
- **GitHub:** https://github.com/SylphxAI

### Location
London, United Kingdom 🇬🇧

---

## 📚 Resources

### Templates
- [README Template](../templates/README-template.md)
- [Package.json Template](../templates/package-template.json)
- [License Template](../templates/LICENSE-MIT)

### Brand Assets
- Logo files: `brand/logos/` (to be added)
- Color palette: [colors.md](colors.md)

---

## 🔄 Updates

This guide is a living document. Suggestions for improvements are welcome.

**Last Updated:** January 2025

---

<p align="center">
  <strong>Consistent branding builds trust</strong>
  <br>
  <sub>© 2025 Sylphx Limited</sub>
  <br><br>
  <a href="https://sylphx.com">sylphx.com</a> •
  <a href="https://x.com/SylphxAI">@SylphxAI</a> •
  <a href="mailto:hi@sylphx.com">hi@sylphx.com</a>
</p>
