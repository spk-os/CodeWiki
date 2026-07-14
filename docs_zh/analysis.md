# 分析模块 (Analysis) 文档

## 概述

**分析模块**是 `dependency_analyzer` 依赖分析系统的核心编排层，负责协调从仓库克隆、文件结构分析到多语言调用图生成的完整工作流。该模块位于 `codewiki/src/be/dependency_analyzer/analysis/` 目录下，包含三个核心组件：

- **[AnalysisService](./analysis_service.md)**：分析服务，提供完整和轻量级分析 API
- **[CallGraphAnalyzer](./call_graph_analyzer.md)**：调用图分析器，协调多语言 AST 解析和关系构建
- **[RepoAnalyzer](./repo_analyzer.md)**：仓库分析器，扫描仓库文件结构并生成文件树

## 架构概览

分析模块在依赖分析系统中的位置如下：

```mermaid
graph TD
    subgraph "依赖分析系统"
        subgraph "分析模块 (当前)"
            AS["AnalysisService<br/>分析服务"] --> CGA["CallGraphAnalyzer<br/>调用图分析器"]
            AS --> RA["RepoAnalyzer<br/>仓库分析器"]
            CGA --> LA["语言特定分析器<br/>(Python/JS/TS/Java/C#/C/C++/PHP/Kotlin)"]
        end
        
        subgraph "支持模块"
            CL["cloning.py<br/>仓库克隆"]
            M["models/<br/>数据模型"]
            U["utils/<br/>工具函数"]
            AP["ast_parser.py<br/>AST 解析器"]
            DG["dependency_graphs_builder.py<br/>依赖图构建器"]
        end
    end
    
    AS --> CL
    AS --> M
    CGA --> LA
    CGA --> U
    CGA --> M
    RA --> U
    LA --> M
```

## 核心功能

| 功能 | 组件 | 描述 |
|------|------|------|
| **仓库结构分析** | RepoAnalyzer | 递归扫描目录，按模式过滤，生成文件树和统计 |
| **多语言调用图** | CallGraphAnalyzer | 支持 9 种语言的 AST 解析，跨语言函数调用关系提取 |
| **完整分析工作流** | AnalysisService | 克隆 → 结构分析 → 调用图生成 → 清理，一站式 API |
| **轻量级分析** | AnalysisService | 仅结构分析，不生成调用图，适用于快速预览 |
| **本地分析** | AnalysisService | 直接分析本地文件夹，无需克隆 |

## 组件关系与数据流

```mermaid
flowchart LR
    subgraph "输入"
        URL[GitHub URL]
        LOCAL[本地路径]
    end
    
    subgraph "分析模块"
        AS[AnalysisService]
        RA[RepoAnalyzer]
        CGA[CallGraphAnalyzer]
    end
    
    subgraph "输出"
        FT[文件树]
        FUNC[函数列表]
        REL[调用关系]
        VIZ[可视化数据]
    end
    
    URL --> AS
    LOCAL --> AS
    AS --> RA
    AS --> CGA
    RA --> FT
    CGA --> FUNC
    CGA --> REL
    CGA --> VIZ
```

## 工作流详解

### 完整分析流程（`analyze_repository_full`）

```mermaid
sequenceDiagram
    participant Client as 客户端
    participant AS as AnalysisService
    participant CL as cloning.py
    participant RA as RepoAnalyzer
    participant CGA as CallGraphAnalyzer
    participant LA as 语言分析器
    
    Client->>AS: analyze_repository_full(github_url)
    
    AS->>CL: clone_repository(url)
    Note over CL: 使用 git clone --depth=1
    CL-->>AS: temp_dir
    
    AS->>RA: analyze_repository_structure(temp_dir)
    Note over RA: 递归构建文件树<br/>include/exclude 过滤
    RA-->>AS: file_tree + summary
    
    AS->>CGA: extract_code_files(file_tree)
    Note over CGA: 按 CODE_EXTENSIONS 过滤
    CGA-->>AS: code_files
    
    AS->>CGA: analyze_code_files(code_files, temp_dir)
    
    loop 每个代码文件
        CGA->>LA: _analyze_xxx_file(path, content)
        Note over LA: Python: AST<br/>其他: tree-sitter
        LA-->>CGA: functions + relationships
    end
    
    CGA->>CGA: _resolve_call_relationships()
    Note over CGA: 精确匹配→简单名称→Java包上下文→外部符号过滤
    CGA->>CGA: _deduplicate_relationships()
    CGA->>CGA: _generate_visualization_data()
    CGA-->>AS: result
    
    AS->>AS: 封装为 AnalysisResult
    AS-->>Client: AnalysisResult
```

### 结构分析流程（`analyze_repository_structure_only`）

```mermaid
sequenceDiagram
    participant Client as 客户端
    participant AS as AnalysisService
    participant CL as cloning.py
    participant RA as RepoAnalyzer
    
    Client->>AS: analyze_repository_structure_only(github_url)
    AS->>CL: clone_repository(url)
    CL-->>AS: temp_dir
    
    AS->>RA: analyze_repository_structure(temp_dir)
    RA-->>AS: file_tree + summary
    
    AS-->>Client: {repository, file_tree, file_summary}
```

### 本地分析流程（`analyze_local_repository`）

```mermaid
sequenceDiagram
    participant Client as 客户端
    participant AS as AnalysisService
    participant RA as RepoAnalyzer
    participant CGA as CallGraphAnalyzer
    
    Client->>AS: analyze_local_repository(repo_path)
    AS->>RA: analyze_repository_structure(repo_path)
    RA-->>AS: structure_result
    
    AS->>CGA: extract_code_files(file_tree)
    CGA-->>AS: code_files
    
    Note over AS: 可选: 按语言过滤、限制文件数
    
    AS->>CGA: analyze_code_files(code_files, repo_path)
    CGA-->>AS: result
    
    AS-->>Client: {nodes, relationships, summary}
```

## 核心模型的数据流

```mermaid
classDiagram
    class AnalysisService {
        +CallGraphAnalyzer call_graph_analyzer
        +List~str~ _temp_directories
        +analyze_repository_full() AnalysisResult
        +analyze_repository_structure_only() Dict
        +analyze_local_repository() Dict
        +cleanup_all()
    }
    
    class CallGraphAnalyzer {
        +Dict~str,Node~ functions
        +List~CallRelationship~ call_relationships
        +analyze_code_files() Dict
        +extract_code_files() List
        +generate_llm_format() Dict
    }
    
    class RepoAnalyzer {
        +List~str~ include_patterns
        +List~str~ exclude_patterns
        +analyze_repository_structure() Dict
    }
    
    AnalysisService --> CallGraphAnalyzer
    AnalysisService --> RepoAnalyzer
    CallGraphAnalyzer --> "产生" Node
    CallGraphAnalyzer --> "产生" CallRelationship
    AnalysisService --> "封装为" AnalysisResult
    AnalysisResult --> Repository
    AnalysisResult --> Node
    AnalysisResult --> CallRelationship
```

## 子模块详细文档

| 文档 | 组件 | 核心类/函数 | 文件 |
|------|------|-------------|------|
| [AnalysisService](./analysis_service.md) | 分析服务 | `AnalysisService` | `analysis_service.py` |
| [CallGraphAnalyzer](./call_graph_analyzer.md) | 调用图分析器 | `CallGraphAnalyzer`, `TimeoutError` | `call_graph_analyzer.py` |
| [RepoAnalyzer](./repo_analyzer.md) | 仓库分析器 | `RepoAnalyzer` | `repo_analyzer.py` |

## 相关模块文档

| 模块 | 文档 | 关系 |
|------|------|------|
| [Models](./models.md) | [核心模型](./models_core.md) · [分析模型](./models_analysis.md) | 分析模块消费 Node、CallRelationship、AnalysisResult |
| [Analyzers](./analyzers.md) | 各语言分析器 | CallGraphAnalyzer 调用各语言分析器解析代码 |
| [Cloning](./cloning.md) | 仓库克隆工具 | AnalysisService 使用 clone_repository 克隆 GitHub 仓库 |
| [Utils](./utils.md) | 工具函数 | 路径安全、模式匹配、外部符号识别 |
| [AST Parser](./ast_parser.md) | AST 解析 | 将分析结果转换为组件依赖图 |
| [Dependency Graph Builder](./dependency_graphs_builder.md) | 依赖图构建 | 消费分析结果构建依赖图 |
