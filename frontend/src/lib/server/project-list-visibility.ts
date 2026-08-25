type ProjectListRow = {
  ownerId?: unknown;
  slug?: unknown;
};

export function isDefaultInboxProject(project: ProjectListRow): boolean {
  return (
    typeof project.ownerId === "string" &&
    typeof project.slug === "string" &&
    project.slug === `inbox-project-${project.ownerId}`
  );
}

export function isForeignDefaultInboxProject(
  project: ProjectListRow,
  userId: string,
): boolean {
  return project.ownerId !== userId && isDefaultInboxProject(project);
}
