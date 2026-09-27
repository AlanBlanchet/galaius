/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

/**
 * One typed, cursor-addressed delta from the prompt catalogue.
 */
export interface PromptCatalogPage {
  entries: PromptChannelEntry[];
  removed?: PromptKey[];
  cursor?: string | null;
  server_timestamp: string;
  [k: string]: unknown;
}
/**
 * Compare-and-swap channel pointer to one immutable prompt revision.
 */
export interface PromptChannelEntry {
  key: PromptKey;
  channel: string;
  revision: string;
  digest: string;
  lock_version: number;
  [k: string]: unknown;
}
/**
 * Stable prompt identity independent of revisions and publication channels.
 */
export interface PromptKey {
  namespace: string;
  slug: string;
  [k: string]: unknown;
}
/**
 * Hostile authoring input for the first immutable Git-backed prompt revision.
 */
export interface PromptCreateRequest {
  key: PromptKey;
  name: string;
  content: string;
}
/**
 * Server-verified immutable prompt identity persisted with one execution.
 */
export interface PromptExecutionRef {
  key: PromptKey;
  channel: string;
  digest: string;
  revision: string;
  [k: string]: unknown;
}
/**
 * Integrity and compare-and-swap metadata for one bounded Git bundle.
 */
export interface PromptGitBundleManifest {
  expected_head?: string | null;
  head: string;
  manifest_digest: string;
  bundle_digest: string;
  bundle_size: number;
  [k: string]: unknown;
}
/**
 * One complete exact-commit prompt snapshot applied by global cursor CAS.
 */
export interface PromptPublicationRequest {
  expected_cursor?: string | null;
  source_commit: string;
  entries: PromptSelection[];
  revisions: PromptRevision[];
}
/**
 * Untrusted channel and digest requested for server-backed resolution.
 */
export interface PromptSelection {
  key: PromptKey;
  channel: string;
  digest: string;
  [k: string]: unknown;
}
/**
 * Immutable prompt content and its verifiable publication provenance.
 */
export interface PromptRevision {
  key: PromptKey;
  name?: string | null;
  revision: string;
  parent_digest?: string | null;
  digest: string;
  content: string;
  source_commit: string;
  created_at: string;
  [k: string]: unknown;
}
/**
 * Safe server-side projection of the latest prompt Git synchronization.
 */
export interface PromptSyncStatus {
  server_head?: string | null;
  last_import_at?: string | null;
  last_export_at?: string | null;
  last_result?: ("succeeded" | "conflict" | "unavailable") | null;
  error_code?: ("head_changed" | "non_fast_forward" | "invalid_bundle" | "repository_unavailable") | null;
  [k: string]: unknown;
}
/**
 * A contract read from the other side: unknown fields are kept (see module docstring).
 */
export interface WireModel {
  [k: string]: unknown;
}
/**
 * A contract only a client authors and sends: an unknown field is refused.
 */
export interface WireRequest {}
