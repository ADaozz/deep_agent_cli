import type { ToolView } from './reducer';

export interface FileChange {
    id: string;
    path: string;
    operation: 'create' | 'overwrite' | 'write' | 'edit' | 'delete';
    before?: string;
    after?: string;
    replaceAll: boolean;
}

/** Completed file-tool operations only; never infer changes from shell/model text. */
export function fileChanges(tools: Record<string, ToolView>): FileChange[] {
    return Object.values(tools).flatMap<FileChange>(tool => {
        const path = tool.arguments.file_path ?? tool.arguments.path;
        if (tool.status !== 'completed' || typeof path !== 'string' || !path.trim())
            return [];
        const base = { id: tool.id, path, replaceAll: false };
        if (tool.name === 'delete')
            return [{ ...base, operation: 'delete' as const }];
        if (tool.name === 'write_file') {
            const artifact = tool.artifact;
            const operation = artifact && typeof artifact === 'object' && 'operation' in artifact
                ? artifact.operation : undefined;
            return [{ ...base,
                operation: operation === 'create' || operation === 'overwrite' ? operation : 'write' as const,
                after: typeof tool.arguments.content === 'string' ? tool.arguments.content : undefined,
            }];
        }
        if (tool.name === 'edit_file') {
            const before = tool.arguments.old_string ?? tool.arguments.old_text;
            const after = tool.arguments.new_string ?? tool.arguments.new_text;
            return [{ ...base, operation: 'edit' as const,
                before: typeof before === 'string' ? before : undefined,
                after: typeof after === 'string' ? after : undefined,
                replaceAll: tool.arguments.replace_all === true,
            }];
        }
        return [];
    });
}
