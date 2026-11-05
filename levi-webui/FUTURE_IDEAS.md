# Future ideas (not scheduled, not built)

Parking lot for real ideas that are not scope for the current work. Nothing
here is a commitment; move an item out once it actually gets scheduled.

## Artifacts panel (Claude-style)

A side panel for generated content (code, documents, diagrams) that the
model writes to directly, separate from the normal chat transcript, the
way Claude's own Artifacts feature works.

Blocked on a real prerequisite: the model running here has no file-write
capability today, only read tools (list_project_folder, read_project_file,
read_project_image). An Artifacts panel without a real write path behind
it would just be a second place to fake content, the same "fabricated
status" problem this codebase's own discipline argues against elsewhere.

Revisit once the model actually gets a real write tool; until then this
stays a note, not a ticket.
