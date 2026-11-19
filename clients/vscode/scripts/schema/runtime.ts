
type WireSchema = {
  $defs?: Record<string, WireSchema>;
  $ref?: string;
  anyOf?: WireSchema[];
  oneOf?: WireSchema[];
  const?: unknown;
  enum?: unknown[];
  type?: string;
  required?: string[];
  properties?: Record<string, WireSchema>;
  items?: WireSchema;
  additionalProperties?: boolean | WireSchema;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minItems?: number;
  maxItems?: number;
  minProperties?: number;
  maxProperties?: number;
};

const conversationSchemas = __CONVERSATION_SCHEMAS__ as Record<string, WireSchema>;

function wireRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function schemaValue(schema: WireSchema, value: unknown, root: WireSchema): boolean {
  if (schema.$ref) {
    const name = schema.$ref.split("/").at(-1);
    return Boolean(name && root.$defs?.[name] && schemaValue(root.$defs[name], value, root));
  }
  if (schema.anyOf && !schema.anyOf.some((choice) => schemaValue(choice, value, root))) return false;
  if (schema.oneOf && schema.oneOf.filter((choice) => schemaValue(choice, value, root)).length !== 1) return false;
  if ("const" in schema && value !== schema.const) return false;
  if (schema.enum && !schema.enum.some((choice) => choice === value)) return false;
  if (schema.type === "null") return value === null;
  if (schema.type === "string" && typeof value !== "string") return false;
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) return false;
    if (schema.maxLength !== undefined && value.length > schema.maxLength) return false;
    if (schema.pattern && !new RegExp(schema.pattern, "u").test(value)) return false;
  }
  if ((schema.type === "number" || schema.type === "integer") && typeof value !== "number") return false;
  if (schema.type === "integer" && typeof value === "number" && !Number.isInteger(value)) return false;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return false;
    if (schema.minimum !== undefined && value < schema.minimum) return false;
    if (schema.maximum !== undefined && value > schema.maximum) return false;
    if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) return false;
    if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) return false;
  }
  if (schema.type === "boolean" && typeof value !== "boolean") return false;
  if (schema.type === "array") {
    if (!Array.isArray(value)) return false;
    if (schema.minItems !== undefined && value.length < schema.minItems) return false;
    if (schema.maxItems !== undefined && value.length > schema.maxItems) return false;
    return !schema.items || value.every((item) => schemaValue(schema.items!, item, root));
  }
  if (schema.type === "object" || schema.properties || schema.required) {
    if (!wireRecord(value)) return false;
    if (schema.minProperties !== undefined && Object.keys(value).length < schema.minProperties) return false;
    if (schema.maxProperties !== undefined && Object.keys(value).length > schema.maxProperties) return false;
    if (schema.required?.some((key) => !(key in value))) return false;
    for (const [key, item] of Object.entries(value)) {
      const property = schema.properties?.[key];
      if (property) {
        if (!schemaValue(property, item, root)) return false;
      } else if (schema.additionalProperties === false) return false;
      else if (wireRecord(schema.additionalProperties)
          && !schemaValue(schema.additionalProperties, item, root)) return false;
    }
  }
  return true;
}

function decodeWire<Type>(name: string, value: unknown): Type {
  const schema = conversationSchemas[name];
  if (!schema || !schemaValue(schema, value, schema)) {
    throw new TypeError(`${name} does not match the generated Python wire contract`);
  }
  return value as Type;
}

export function decodeConversationCommand(value: unknown): ConversationCommand {
  return decodeWire<ConversationCommand>("ConversationCommand", value);
}

export function decodeConversationResponse(value: unknown): ConversationResponse {
  return decodeWire<ConversationResponse>("ConversationResponse", value);
}

export function decodeConversationStreamEvent(value: unknown): ConversationStreamEvent {
  return decodeWire<ConversationStreamEvent>("ConversationStreamEvent", value);
}

export function decodeAgentRun(value: unknown): AgentRun {
  return decodeWire<AgentRun>("AgentRun", value);
}

export function decodeAgentEvent(value: unknown): AgentEvent {
  return decodeWire<AgentEvent>("AgentEvent", value);
}
