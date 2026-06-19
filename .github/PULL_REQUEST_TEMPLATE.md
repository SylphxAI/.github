## 📝 Description

<!-- Provide a clear and concise description of your changes -->

## 🔗 Related Issues

<!-- Link related issues using keywords: Fixes #123, Closes #456, Relates to #789 -->

- Fixes #

## 🎯 Type of Change

<!-- Mark the relevant option with an [x] -->

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 💥 Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] 📚 Documentation update
- [ ] ♻️ Code refactoring (no functional changes)
- [ ] ⚡ Performance improvement
- [ ] ✅ Test update
- [ ] 🔧 Build/tooling change


## 🤖 Agent-first Delivery Gate Evidence

<!-- Required for Agent-first workflow PRs. Mark N/A only when the repo owner agrees the gate does not apply. -->

### pr-metadata/pass
- [ ] Linked issue, ADR, spec, or manager instruction is referenced above
- [ ] Project boundary/source of truth read (`PROJECT_BOUNDARY`, `AGENT_GUIDE`, `README`, ADR/spec where present)
- [ ] Change scope is separated by repo/project boundary
- [ ] Required checks expected for this repo are listed or linked

### risk-classification/pass
- [ ] Risk tier selected: `low` / `normal` / `high` / `strict`
- [ ] High/strict risk evidence path documented before merge
- [ ] No direct default-branch push, force-push, destructive settings change, or secret exposure

### ai-review/pass
- [ ] AI/self review completed and summarized
- [ ] Independent review requested when risk or policy requires it
- [ ] Open AI review findings are linked or explicitly marked none

## 🧪 Testing

<!-- Describe how you tested your changes -->

### Test Coverage
- [ ] All existing tests pass
- [ ] Added new tests for new features
- [ ] Updated tests for changed features
- [ ] Manual testing completed

### Test Details
<!-- Describe your testing approach -->

```bash
# Commands used for testing
npm test
npm run lint
npm run typecheck
```

## 📋 Checklist

<!-- Mark completed items with an [x] -->

### Code Quality
- [ ] Code follows project style guidelines
- [ ] Self-review completed
- [ ] Code is properly commented
- [ ] No unnecessary console.logs or debug code

### Documentation
- [ ] README updated (if needed)
- [ ] API documentation updated (if needed)
- [ ] Examples updated (if needed)
- [ ] CHANGELOG updated

### Dependencies
- [ ] No new dependencies added
- [ ] New dependencies are necessary and justified
- [ ] Dependencies are properly documented

### Breaking Changes
- [ ] No breaking changes
- [ ] Breaking changes are documented
- [ ] Migration guide provided (if needed)

## 📸 Screenshots / Examples

<!-- If applicable, add screenshots or code examples -->

### Before
```typescript
// Old code/behavior
```

### After
```typescript
// New code/behavior
```

## ⚡ Performance Impact

<!-- Describe any performance implications -->

- [ ] No performance impact
- [ ] Performance improved
- [ ] Performance regression (explain why acceptable)

## 🔐 Security Considerations

<!-- Describe any security implications -->

- [ ] No security impact
- [ ] Security review completed
- [ ] No sensitive data exposed

## 📝 Additional Notes

<!-- Any additional information for reviewers -->

---

## 🙏 Reviewer Checklist

<!-- For maintainers reviewing the PR -->

- [ ] Code quality is acceptable
- [ ] Tests are comprehensive
- [ ] Documentation is complete
- [ ] No security concerns
- [ ] Performance is acceptable
- [ ] Breaking changes are justified
