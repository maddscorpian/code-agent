import * as vscode from "vscode";

const BASE_URL = "http://localhost:8765";

export interface AskResponse {
  answer: string;
  mode: string;
  sources: Array<{ file_path: string; project: string; type: string; preview: string }>;
  duration_ms: number;
}

export interface ReindexResponse {
  status: string;
  projects_indexed: string[];
  chunks_created: number;
  duration_ms: number;
}

export interface HealthResponse {
  status: string;
  ollama: boolean;
  chromadb: boolean;
  model: string;
}

export interface DigestResponse {
  projects: string[];
  total_endpoints: number;
  total_entities: number;
  last_digest_at: string;
}

export class ApiClient {
  async ask(question: string, mode: string, fileContext?: string): Promise<AskResponse> {
    return this._request("/ask", "POST", { question, mode, file_context: fileContext });
  }

  async askStream(question: string, mode: string, fileContext: string | undefined, onToken: (token: string) => void): Promise<void> {
    try {
      const response = await fetch(`${BASE_URL}/ask/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, mode, file_context: fileContext }),
      });
      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
            break;
        }
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() || "";
        for (const ev of events) {
          if (ev.startsWith("data: ")) {
            onToken(ev.slice(6));
          }
        }
      }
    } catch (err) {
      vscode.window.showErrorMessage("Local AI Agent server is not running or unreachable.");
      throw err;
    }
  }

  async reindex(project?: string): Promise<ReindexResponse> {
    return this._request("/reindex", "POST", { project });
  }

  async getHealth(): Promise<HealthResponse> {
    return this._request("/health", "GET");
  }

  async getDigestSummary(): Promise<DigestResponse> {
    return this._request("/digest", "GET");
  }

  private async _request(path: string, method: string, body?: unknown): Promise<any> {
    try {
      const res = await fetch(`${BASE_URL}${path}`, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      return await res.json();
    } catch (err) {
      vscode.window.showErrorMessage("Local AI Agent server is not running or unreachable.");
      throw err;
    }
  }
}
