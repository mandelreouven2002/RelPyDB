---
name: documentation
about: Report unclear, missing, or incorrect documentation
title: ''
labels: ''
assignees: ''

---

name: Documentation issue
description: Report unclear, missing, or incorrect documentation
title: "[Docs]: "
labels: ["documentation", "needs-triage"]
body:
  - type: textarea
    id: page
    attributes:
      label: Page or section
      description: Which documentation page or section is affected?
      placeholder: Quickstart, Joins, Function Reference...
    validations:
      required: true

  - type: textarea
    id: issue
    attributes:
      label: What is unclear or incorrect?
      description: Explain the problem with the documentation.
    validations:
      required: true

  - type: textarea
    id: suggestion
    attributes:
      label: Suggested improvement
      description: Optional suggested wording or example.
