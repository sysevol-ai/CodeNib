import pickle

import igraph as ig
import matplotlib.pyplot as plt

from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    is_symbol_node,
)


class CodeGraph:
    """
    A class to represent and manipulate a code graph using igraph.
    start_line uses 0-based index.
    """

    def __init__(self, project_root=None):
        # Create a directed graph
        self.graph = ig.Graph(directed=True)
        self.current_file = None
        self.current_scope = None
        self.project_root = project_root
        self.scope_stack = []  # List of {symbol: [start_line, end_line]} dicts
        # Store line ranges for symbols
        self.symbol_ranges = {}
        # Map symbol names to vertex IDs
        self.name_to_vertex = {}

    def add_file_node(self, file_path):
        """
        Add a file node to the graph and set it as the current file and scope.

        Args:
            file_path: Path of the file to add
        """
        self.current_file = file_path

        # Add vertex for file
        self._add_vertex(file_path, {"type": NODE_TYPE_FILE})

        self.current_scope = file_path
        # File scope has no range (special case)
        self.scope_stack = [{file_path: None}]

    def add_symbol_node(
        self, symbol, line, scope_start_line=None, scope_end_line=None, symbol_type=None
    ):
        """
        Add a symbol node to the graph.

        Args:
            symbol: Symbol name
            line: Line number of the symbol
            scope_start_line: Start line of the symbol's scope (optional)
            scope_end_line: End line of the symbol's scope (optional)
            symbol_type: Type of symbol (NODE_TYPE_CLASS, NODE_TYPE_METHOD, NODE_TYPE_FUNCTION) (optional)
        """
        # Use specific symbol type if provided, otherwise default to generic symbol
        node_type = symbol_type if symbol_type else NODE_TYPE_SYMBOL

        if scope_start_line is not None and scope_end_line is not None:
            # Store symbol range
            self.symbol_ranges[symbol] = (scope_start_line, scope_end_line)

            # Add symbol vertex with scope range
            self._add_vertex(
                symbol,
                {
                    "type": node_type,
                    "file": self.current_file,
                    "start_line": scope_start_line,
                    "end_line": scope_end_line,
                },
            )
        else:
            # Add symbol vertex without scope range
            self._add_vertex(
                symbol,
                {
                    "type": node_type,
                    "file": self.current_file,
                    "start_line": line,
                    "end_line": line,
                },
            )

    def add_symbol_reference(self, symbol, module_path=None, symbol_type=None):
        """
        Add a reference to a symbol.

        Args:
            symbol: Symbol being referenced
            module_path: Path of the module containing the symbol (optional)
            symbol_type: Type of symbol (NODE_TYPE_CLASS, NODE_TYPE_METHOD, NODE_TYPE_FUNCTION) (optional)
        """
        # If the symbol doesn't exist, create it without range info
        if symbol not in self.name_to_vertex:
            file_attr = module_path if module_path else None
            node_type = symbol_type if symbol_type else NODE_TYPE_SYMBOL
            self._add_vertex(symbol, {"type": node_type, "file": file_attr})

        # Add reference edge
        self._add_edge(self.current_scope, symbol, EDGE_TYPE_REFERENCE)

    def update_current_scope(self, symbol, start_line=None, end_line=None):
        """
        Update the current scope to the given symbol with its range.

        Args:
            symbol: Symbol to set as current scope
            start_line: Start line of the symbol's scope
            end_line: End line of the symbol's scope
        """
        self.current_scope = symbol
        # Add symbol with its range to scope stack
        scope_range = (
            [start_line, end_line]
            if start_line is not None and end_line is not None
            else None
        )
        self.scope_stack.append({symbol: scope_range})

    def exit_scopes_by_line(self, current_line):
        """
        Exit scopes that have ended based on current line number.
        File scope (with None range) is never popped.

        Args:
            current_line: Current line number being processed
        """
        # Pop scopes whose range has ended
        while len(self.scope_stack) > 1:  # Keep at least the file scope
            top_scope_dict = self.scope_stack[-1]
            scope_symbol = list(top_scope_dict.keys())[0]
            scope_range = top_scope_dict[scope_symbol]

            # If scope has no range (file scope), don't pop
            if scope_range is None:
                break

            # If current line is beyond scope's end, pop it
            start_line, end_line = scope_range
            if current_line > end_line:
                self.scope_stack.pop()
                # Update current_scope to new top of stack
                if self.scope_stack:
                    new_top = self.scope_stack[-1]
                    self.current_scope = list(new_top.keys())[0]
                else:
                    self.current_scope = self.current_file
            else:
                # Scope is still active
                break

    def add_containment_edge(self, target_symbol):
        """
        Add a containment edge from current scope to a symbol.

        Args:
            target_symbol: Symbol being contained
        """
        # Use the current scope directly (not parent scope)
        parent_scope = self.current_scope
        self._add_edge(parent_scope, target_symbol, EDGE_TYPE_CONTAIN)

    def _add_vertex(self, name, attributes=None):
        """
        Add a vertex to the graph if it doesn't exist.

        Args:
            name: Name of the vertex
            attributes: Dictionary of vertex attributes

        Returns:
            Vertex ID
        """
        if name in self.name_to_vertex:
            vertex_id = self.name_to_vertex[name]
            # Update attributes if provided
            if attributes:
                for key, value in attributes.items():
                    self.graph.vs[vertex_id][key] = value
            return vertex_id

        # Add a new vertex
        self.graph.add_vertices(1)
        vertex_id = self.graph.vcount() - 1
        self.name_to_vertex[name] = vertex_id

        # Set the name attribute
        self.graph.vs[vertex_id]["name"] = name

        # Set other attributes if provided
        if attributes:
            for key, value in attributes.items():
                self.graph.vs[vertex_id][key] = value

        return vertex_id

    def _add_edge(self, source_name, target_name, edge_type):
        """
        Add an edge between two vertices.

        Args:
            source_name: Name of the source vertex
            target_name: Name of the target vertex
            edge_type: Type of the edge (e.g., "reference", "contain")

        Returns:
            Edge ID
        """
        # Make sure both vertices exist
        source_id = (
            self._add_vertex(source_name)
            if source_name not in self.name_to_vertex
            else self.name_to_vertex[source_name]
        )
        target_id = (
            self._add_vertex(target_name)
            if target_name not in self.name_to_vertex
            else self.name_to_vertex[target_name]
        )

        # Check if the edge already exists
        if self.graph.are_adjacent(source_id, target_id):
            # Edge already exists, return its ID
            edge_id = self.graph.get_eid(source_id, target_id, error=False)
            if edge_id is not None:
                return edge_id

        # Add edge
        self.graph.add_edges([(source_id, target_id)])
        edge_id = self.graph.ecount() - 1

        # Set edge type
        self.graph.es[edge_id]["type"] = edge_type

        return edge_id

    def add_root_node(self, project_root):
        """Add the root node to the graph"""
        self._add_vertex(project_root, {"type": "root"})

    def add_directory_node(self, dir_path):
        """Add a directory node to the graph"""
        self._add_vertex(dir_path, {"type": NODE_TYPE_DIRECTORY})

    def save_graph(self, output_path):
        """
        Save the graph to a GraphML file.

        Args:
            output_path: Path to save the GraphML file
        """
        # Use igraph's built-in GraphML writer
        self.graph.write_graphml(output_path)

    @classmethod
    def load_graph(cls, input_path, project_root=None):
        """
        Load a graph from a GraphML file.

        Args:
            input_path: Path to the GraphML file
            project_root: Optional project root path

        Returns:
            CodeGraph: Loaded graph instance
        """
        # Create new CodeGraph instance
        graph_instance = cls(project_root=project_root)

        # Load the igraph from GraphML
        graph_instance.graph = ig.Graph.Read_GraphML(input_path)

        # Rebuild name_to_vertex mapping
        graph_instance.name_to_vertex = {}
        for v in graph_instance.graph.vs:
            if "name" in v.attributes():
                graph_instance.name_to_vertex[v["name"]] = v.index

        # Rebuild symbol_ranges from vertex attributes
        graph_instance.symbol_ranges = {}
        for v in graph_instance.graph.vs:
            if "name" in v.attributes() and "start_line" in v.attributes() and "end_line" in v.attributes():
                name = v["name"]
                start_line = v["start_line"]
                end_line = v["end_line"]
                # Filter out None and NaN values
                import math
                if (start_line is not None and end_line is not None and
                    not (isinstance(start_line, float) and math.isnan(start_line)) and
                    not (isinstance(end_line, float) and math.isnan(end_line))):
                    graph_instance.symbol_ranges[name] = (int(start_line), int(end_line))

        return graph_instance

    # Old pickle-based implementation (commented out for reference)
    # def save_graph(self, output_path):
    #     """
    #     Save the graph to a pickle file for fast serialization.
    #
    #     Args:
    #         output_path: Path to save the pickle file
    #     """
    #     # Prepare data for pickling
    #     data = {
    #         "project_root": str(self.project_root) if self.project_root else None,
    #         "graph": self.graph,  # igraph objects are picklable
    #         "symbol_ranges": self.symbol_ranges,
    #         "name_to_vertex": self.name_to_vertex,
    #     }
    #
    #     with open(output_path, "wb") as f:
    #         pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    #
    # @classmethod
    # def load_graph(cls, input_path):
    #     """
    #     Load a graph from a pickle file.
    #
    #     Args:
    #         input_path: Path to the pickle file
    #
    #     Returns:
    #         CodeGraph: Loaded graph instance
    #     """
    #     with open(input_path, "rb") as f:
    #         data = pickle.load(f)
    #
    #     # Create new CodeGraph instance
    #     graph_instance = cls(project_root=data.get("project_root"))
    #
    #     # Restore the igraph object and internal state
    #     graph_instance.graph = data["graph"]
    #     graph_instance.symbol_ranges = data.get("symbol_ranges", {})
    #     graph_instance.name_to_vertex = data.get("name_to_vertex", {})
    #
    #     return graph_instance

    def get_graph(self):
        """
        Get the igraph Graph object.

        Returns:
            The igraph Graph instance
        """
        return self.graph

    def get_node_info_by_name(self, node_name):
        """
        Get information about a node in the graph.

        Args:
            node_name: Name of the node

        Returns:
            Dictionary with vertex attributes or None if not found
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return None

        return self.graph.vs[vertex].attributes()

    def get_node_info_by_id(self, node_id):
        """
        Get information about a node in the graph.

        Args:
            node_id: ID of the node

        Returns:
            Dictionary with vertex attributes or None if not found
        """
        if isinstance(node_id, int):
            vertex = self.graph.vs[node_id]
            return vertex.attributes()

        return None

    def get_node_content(self, node_id):
        """
        Get the content of a node in the graph.
        Read the file from start_line to end_line.
        If the node is a file, return the file content.

        Args:
            node_id: ID of the node

        Returns:
            Content of the node or None if not found
        """
        node = self.graph.vs[node_id]
        node_type = node["type"] if "type" in node.attributes() else "unknown"
        if node_type == NODE_TYPE_FILE:
            # file path need to add self.project_root
            file_path = node["name"]
            if self.project_root:
                file_path = f"{self.project_root}/{file_path}"
            try:
                with open(file_path, "r") as f:
                    content = f.read()
                return content
            except FileNotFoundError:
                print(f"File not found: {file_path}")
                return None
        elif is_symbol_node(node_type):
            # Get the start and end lines
            start_line = node["start_line"]
            end_line = node["end_line"]

            # Get the file path
            file_path = node["file"] if "file" in node.attributes() else None
            if self.project_root and file_path:
                file_path = f"{self.project_root}/{file_path}"

            # Read the file content
            if file_path:
                try:
                    with open(file_path, "r") as f:
                        lines = f.readlines()
                    return "".join(lines[start_line - 1 : end_line])
                except FileNotFoundError:
                    print(f"File not found: {file_path}")
                    return None
        return None

    def get_neighbors(self, node_name):
        """
        Get the neighbors of a node in the graph.

        Args:
            node_name: Name of the node

        Returns:
            List of neighbor vertex IDs
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return []

        return self.graph.neighbors(vertex)

    def get_successors(self, node_name):
        """
        Get the successors of a node_name (outgoing edges).

        Args:
            node_name: Name of the node

        Returns:
            List of successor vertex IDs
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return []

        return self.graph.successors(vertex)

    def get_predecessors(self, node_name):
        """
        Get the predecessors of a node_name (incoming edges).

        Args:
            node_name: Name of the node

        Returns:
            List of predecessor vertex IDs
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return []

        return self.graph.predecessors(vertex)

    def print_graph_basic_info(self):
        """
        Print basic information about the graph.
        """
        print("Graph Summary:")
        print(f"  Number of vertices: {self.graph.vcount()}")
        print(f"  Number of edges: {self.graph.ecount()}")
        print(f"  Directed: {self.graph.is_directed()}")

        # Print vertex types
        vertex_types = self.graph.vs["type"]
        unique_vertex_types = set(vertex_types)
        print(f"  Unique vertex types: {unique_vertex_types}")

        # Print edge types
        edge_types = self.graph.es["type"]
        unique_edge_types = set(edge_types)
        print(f"  Unique edge types: {unique_edge_types}")

    def visualize_graph(
        self, output_path=None, width=800, height=600, layout="fruchterman_reingold"
    ):
        """
        Visualize the code graph with different colors for different node and edge types.

        Args:
            output_path: Path to save the visualization (optional)
            width: Width of the plot in pixels
            height: Height of the plot in pixels
            layout: Layout algorithm to use ('fruchterman_reingold', 'kk', 'grid', etc.)

        Returns:
            A matplotlib figure object if output_path is not provided
        """
        if self.graph.vcount() == 0:
            print("Graph is empty. Nothing to visualize.")
            return None

        # Define color schemes
        node_type_colors = {
            NODE_TYPE_FILE: "skyblue",
            NODE_TYPE_SYMBOL: "lightgreen",
            NODE_TYPE_CLASS: "gold",
            NODE_TYPE_FUNCTION: "lightgreen",
            NODE_TYPE_METHOD: "lightcoral",
            NODE_TYPE_FIELD: "plum",
            NODE_TYPE_DIRECTORY: "orange",  # Add directory node color
            "root": "lightgrey",  # Root node color
            # Add more node types and colors as needed
        }

        edge_type_colors = {
            EDGE_TYPE_REFERENCE: "red",
            EDGE_TYPE_CONTAIN: "blue",
            # Add more edge types and colors as needed
        }

        # Define visual style
        visual_style = {}

        # Set vertex colors based on type
        vertex_colors = []
        for vertex in self.graph.vs:
            node_type = vertex["type"] if "type" in vertex.attributes() else "unknown"
            vertex_colors.append(node_type_colors.get(node_type, "grey"))
        visual_style["vertex_color"] = vertex_colors

        # Set vertex labels
        visual_style["vertex_label"] = [
            v["name"].split("/")[-1] if "/" in v["name"] else v["name"]
            for v in self.graph.vs
        ]
        visual_style["vertex_label_size"] = 8

        # Set vertex sizes (files can be larger than symbols)
        vertex_sizes = []
        for vertex in self.graph.vs:
            if "type" in vertex.attributes() and vertex["type"] == NODE_TYPE_FILE:
                vertex_sizes.append(20)
            else:
                vertex_sizes.append(10)
        visual_style["vertex_size"] = vertex_sizes

        # Set edge colors based on type
        edge_colors = []
        for edge in self.graph.es:
            edge_type = edge["type"] if "type" in edge.attributes() else "unknown"
            edge_colors.append(edge_type_colors.get(edge_type, "grey"))
        visual_style["edge_color"] = edge_colors

        # Set edge width
        visual_style["edge_width"] = 1.0

        # Calculate layout
        if layout == "fruchterman_reingold":
            visual_style["layout"] = self.graph.layout_fruchterman_reingold()
        elif layout == "kk":
            visual_style["layout"] = self.graph.layout_kamada_kawai()
        elif layout == "grid":
            visual_style["layout"] = self.graph.layout_grid()
        else:
            visual_style["layout"] = self.graph.layout(layout)

        # Adjust dimensions
        visual_style["bbox"] = (width, height)
        visual_style["margin"] = 40

        # Create legend data
        legend_data = {
            "Node Types": [
                (color, node_type) for node_type, color in node_type_colors.items()
            ],
            "Edge Types": [
                (color, edge_type) for edge_type, color in edge_type_colors.items()
            ],
        }

        # Create the plot
        fig, ax = plt.subplots(figsize=(width / 100, height / 100))

        # Plot the graph
        ig.plot(self.graph, target=ax, **visual_style)

        # Add legend for node types
        node_legend_patches = []
        for color, label in legend_data["Node Types"]:
            node_legend_patches.append(
                plt.Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="w",
                    markerfacecolor=color,
                    markersize=10,
                    label=label,
                )
            )

        # Add legend for edge types
        edge_legend_patches = []
        for color, label in legend_data["Edge Types"]:
            edge_legend_patches.append(
                plt.Line2D([0], [0], color=color, lw=2, label=label)
            )

        # Add legends to plot
        ax.legend(
            handles=node_legend_patches + edge_legend_patches,
            loc="upper right",
            title="Legend",
            frameon=True,
        )

        # Save or show the plot
        if output_path:
            plt.savefig(output_path, dpi=100, bbox_inches="tight")
            print(f"Graph visualization saved to {output_path}")
            plt.close(fig)
            return None
        else:
            plt.tight_layout()
            return fig
