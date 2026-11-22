import {
  getArgumentValues,
  getNamedType,
  getNullableType,
  getOperationAST,
  getVariableValues,
  GraphQLError,
  isListType,
  Kind,
} from "postgraphile/graphql";

const MAX_PAGE_SIZE = 1000;
const MAX_OFFSET = 10000;
const MAX_DEPTH = 12;
const MAX_COST = 20000;
const AGGREGATE_COST = 200;

function queryLimit(message) {
  return new GraphQLError(message, { extensions: { code: "QUERY_LIMIT" } });
}

function connectionSize(field, args) {
  const bounds = [args.first, args.last].filter((size) => size != null);
  if (bounds.length !== 1 || bounds[0] < 0 || bounds[0] > MAX_PAGE_SIZE) {
    throw queryLimit(`${field} requires one of first or last between 0 and ${MAX_PAGE_SIZE}`);
  }
  if ((args.offset ?? 0) > MAX_OFFSET) {
    throw queryLimit(`${field} offset exceeds ${MAX_OFFSET}, use a cursor`);
  }
  return bounds[0];
}

function spend(context, cost) {
  context.budget -= cost;
  if (context.budget < 0) throw queryLimit(`Operation exceeds query budget ${MAX_COST}`);
}

function visitField(context, selection, { type, depth, count, pageSize }) {
  const name = selection.name.value;
  const field = type.getFields?.()[name];
  // Schema introspection fields have no backing database collection.
  if (!field) return;
  if (name === "groupedAggregates") {
    throw queryLimit("Grouped aggregates require a private query endpoint");
  }

  const args = getArgumentValues(field, selection, context.variables);
  const paginated = field.args.some((argument) => argument.name === "first");
  const childPageSize = paginated ? connectionSize(name, args) : args.pageSize;
  const rows = isListType(getNullableType(field.type))
    ? Math.min(MAX_PAGE_SIZE, Math.max(0, childPageSize ?? pageSize ?? MAX_PAGE_SIZE)) : 1;
  const aggregateCost = ["aggregates", "totalCount"].includes(name) ? AGGREGATE_COST - 1 : 0;
  // Charge scalar arrays for each additional element as well.
  spend(context, (aggregateCost + Math.max(0, rows - 1)) * count);
  visit(context, selection.selectionSet, {
    type: getNamedType(field.type), depth, count: count * rows, pageSize: childPageSize,
  });
}

function visit(context, selectionSet, scope) {
  if (!selectionSet) return;
  if (scope.depth > MAX_DEPTH) throw queryLimit(`Operation exceeds depth ${MAX_DEPTH}`);
  const childScope = { ...scope, depth: scope.depth + 1 };
  for (const selection of selectionSet.selections) {
    spend(context, Math.max(scope.count, 1));
    if (selection.kind === Kind.FIELD) {
      visitField(context, selection, childScope);
      continue;
    }
    const fragment = selection.kind === Kind.FRAGMENT_SPREAD
      ? context.fragments.get(selection.name.value) : selection;
    const type = fragment.typeCondition
      ? context.schema.getType(fragment.typeCondition.name.value) : scope.type;
    visit(context, fragment.selectionSet, { ...childScope, type });
  }
}

function enforceBudget(schema, document, operation, variables) {
  const fragments = new Map(document.definitions
    .filter((definition) => definition.kind === Kind.FRAGMENT_DEFINITION)
    .map((fragment) => [fragment.name.value, fragment]));
  visit(
    { schema, fragments, variables, budget: MAX_COST },
    operation.selectionSet,
    { type: schema.getQueryType(), depth: 1, count: 1, pageSize: undefined },
  );
}

export const QueryLimitsPlugin = {
  name: "XianQueryLimits",
  version: "1.0.0",
  grafast: {
    middleware: {
      execute(next, { args }) {
        const { schema, document, operationName, variableValues } = args;
        const operation = getOperationAST(document, operationName);
        if (operation?.operation !== "query") return next();

        const { coerced, errors } = getVariableValues(
          schema, operation.variableDefinitions ?? [], variableValues ?? {},
        );
        if (errors) return { errors };
        try {
          enforceBudget(schema, document, operation, coerced);
        } catch (error) {
          if (error instanceof GraphQLError) return { errors: [error] };
          throw error;
        }
        return next();
      },
    },
  },
};
