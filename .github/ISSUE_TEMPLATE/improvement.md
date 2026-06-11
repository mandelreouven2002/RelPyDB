---
name: improvement
about: Suggest an improvement to existing behavior
title: ''
labels: ''
assignees: ''

---

name: Improvement
description: Suggest an improvement to existing behavior
title: "[Improvement]: "
labels: ["improvement", "needs-triage"]
body:
  - type: textarea
    id: current
    attributes:
      label: Current behavior
      description: What currently feels unclear, limited, or inconvenient?
    validations:
      required: true

  - type: textarea
    id: suggested
    attributes:
      label: Suggested improvement
      description: What should be improved?
    validations:
      required: true

  - type: textarea
    id: example
    attributes:
      label: Example
      description: Optional code example.
      render: python
