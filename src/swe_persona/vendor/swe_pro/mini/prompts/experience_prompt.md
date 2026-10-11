# Interaction experience judge (optional, tb3-requirement-loop-v1)

After the conversation has ended, score how the interaction felt for the user described
in `persona`. This score is recorded separately: it never moves the requirement loop and
never changes whether the trial passed.

You see the persona, the experience rubric items and the user-visible conversation, each
message with an `event_id`. You cannot see code, files, tool output or test results.

For each rubric item give a `status`:

- **satisfied**: agent messages show the item holds; quote them.
- **violated**: agent messages show the item does not hold; quote them.
- **unsupported**: the conversation does not show either way.

Every evidence item is an `event_id` of an agent message and a `quote` copied character for
character from it. Then give an overall `satisfaction` from 1 (very poor) to 5 (excellent)
for this user, and a short `reason`.
