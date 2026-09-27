/** Restore a finite set of process-environment keys after a scoped test mutation. */
export function restoreEnvironmentAfter(keys: readonly string[]): () => void {
  const before = keys.map((key) => ({
    key,
    present: Object.hasOwn(process.env, key),
    value: process.env[key],
  }));
  return () => {
    for (const entry of before) {
      if (!entry.present) delete process.env[entry.key];
      else process.env[entry.key] = entry.value!;
    }
  };
}
