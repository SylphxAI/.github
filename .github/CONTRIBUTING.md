# Contributing to Sylphx Projects

**Thank you for considering contributing to Sylphx!** 🎉

We welcome contributions from everyone. By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## 🚀 Quick Start

1. **Fork the repository**
2. **Clone your fork**
   ```bash
   git clone https://github.com/YOUR-USERNAME/repo-name.git
   cd repo-name
   ```
3. **Install dependencies**
   ```bash
   npm install
   # or
   bun install
   ```
4. **Create a feature branch**
   ```bash
   git checkout -b feature/my-feature
   ```

---

## 📝 Contribution Guidelines

### Before You Start

- **Open an issue first** - Discuss your proposed changes before implementing
- **Check existing issues** - Your idea might already be in progress
- **Review the roadmap** - Ensure your contribution aligns with project goals

### Code Standards

#### TypeScript
- ✅ Use strict TypeScript with full type coverage
- ✅ Follow functional programming principles
- ✅ Prefer immutability and pure functions
- ✅ Use meaningful variable and function names

#### Code Quality
```bash
# Run all quality checks
npm run lint        # Linting
npm run typecheck   # Type checking
npm test            # Tests
npm run build       # Build verification
```

#### Testing
- ✅ Write tests for all new features
- ✅ Maintain or improve code coverage
- ✅ Test edge cases and error conditions
- ✅ Use descriptive test names

```typescript
// ✅ Good
test('should parse valid PDF and extract text content', async () => {
  // ...
});

// ❌ Bad
test('test1', () => {
  // ...
});
```

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types:**
- `feat` - New feature
- `fix` - Bug fix
- `docs` - Documentation only
- `style` - Code style changes (formatting, etc.)
- `refactor` - Code refactoring
- `test` - Adding or updating tests
- `chore` - Maintenance tasks

**Examples:**
```bash
feat(pdf): add support for encrypted PDFs
fix(mcp): handle connection timeout gracefully
docs(readme): update installation instructions
test(parser): add edge case tests for malformed input
```

### Pull Request Process

1. **Update documentation** - README, API docs, examples
2. **Add tests** - Ensure good coverage
3. **Run quality checks** - All checks must pass
4. **Update CHANGELOG** - Document your changes
5. **Link related issues** - Reference issue numbers

#### PR Template
```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] All tests pass
- [ ] Added new tests
- [ ] Manual testing completed

## Checklist
- [ ] Code follows project style
- [ ] Documentation updated
- [ ] No breaking changes (or documented)
- [ ] CHANGELOG updated
```

---

## 🏗️ Project Structure

```
repo-name/
├── src/           # Source code
├── tests/         # Test files
├── docs/          # Documentation
├── examples/      # Usage examples
├── dist/          # Build output (generated)
└── package.json   # Package configuration
```

---

## 🔍 Code Review

### What We Look For

✅ **Code Quality**
- Readable and maintainable
- Follows project conventions
- Properly typed (TypeScript)
- Well-tested

✅ **Performance**
- No unnecessary complexity
- Efficient algorithms
- Proper error handling

✅ **Documentation**
- Clear comments for complex logic
- Updated API documentation
- Examples for new features

---

## 🐛 Reporting Bugs

### Before Reporting
- **Search existing issues** - Bug might already be reported
- **Try latest version** - Bug might be fixed
- **Reproduce the bug** - Ensure it's reproducible

### Bug Report Template
```markdown
## Bug Description
Clear description of the bug

## Steps to Reproduce
1. Step one
2. Step two
3. ...

## Expected Behavior
What you expected to happen

## Actual Behavior
What actually happened

## Environment
- OS: [e.g., macOS 14.0]
- Node.js: [e.g., v20.0.0]
- Package version: [e.g., 1.2.3]

## Additional Context
Screenshots, error logs, etc.
```

---

## 💡 Feature Requests

### Before Requesting
- **Check roadmap** - Feature might be planned
- **Search issues** - Feature might be requested
- **Consider scope** - Should it be in this project?

### Feature Request Template
```markdown
## Feature Description
Clear description of the proposed feature

## Use Case
Why is this feature needed?

## Proposed Solution
How should it work?

## Alternatives Considered
Other approaches you've thought about

## Additional Context
Examples, mockups, references
```

---

## 🎓 Development Tips

### Local Development
```bash
# Watch mode for development
npm run dev

# Run tests in watch mode
npm run test:watch

# Check types continuously
npm run typecheck -- --watch
```

### Debugging
```bash
# Run with debug logging
DEBUG=* npm run dev

# Node.js inspector
node --inspect node_modules/.bin/vitest
```

### Performance Testing
```bash
# Run benchmarks
npm run bench
```

---

## 📚 Resources

### Documentation
- [Project README](../README.md)
- [API Documentation](../docs/)
- [Examples](../examples/)

### Community
- [GitHub Discussions](https://github.com/SylphxAI/repo-name/discussions)
- [Issue Tracker](https://github.com/SylphxAI/repo-name/issues)

### Contact
- **Email**: hi@sylphx.com
- **Twitter**: [@SylphxAI](https://x.com/SylphxAI)
- **Website**: [sylphx.com](https://sylphx.com)

---

## ⚖️ License

By contributing, you agree that your contributions will be licensed under the same [MIT License](../LICENSE) that covers the project.

---

## 🙏 Recognition

Contributors will be:
- ✅ Listed in CHANGELOG
- ✅ Credited in release notes
- ✅ Added to README contributors section (for significant contributions)

---

<p align="center">
  <strong>Thank you for contributing to Sylphx! 🚀</strong>
  <br>
  <sub>Every contribution makes a difference</sub>
  <br><br>
  <a href="https://sylphx.com">sylphx.com</a> •
  <a href="https://x.com/SylphxAI">@SylphxAI</a> •
  <a href="mailto:hi@sylphx.com">hi@sylphx.com</a>
</p>
