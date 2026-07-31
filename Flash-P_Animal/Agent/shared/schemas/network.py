"""
FLASH-P **Light** schemas for Step 2 (BUILDER) output files.

- network.json            -> nodes {id,ty,fn,src} + edges {s,t,x,eid,d}
- algebraic_equations.json -> equations {n,ty,src,a,inh,f} + readable `parameters`
- ode_equations.json       -> equations {n,a,inh,f}
- node_annotations.json    -> annotations {n,fn,ty,desc,src}   (degrees recomputable -> dropped)

`effect`/`mechanism`/`evidence` are gone from edges; `effect` is derived from
`sign` at export/cytoscape. See ../LEXICON.md.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import Field

from .common import DoiStr, FlashPMetadata, NodeType, SlimModel


# ============================================================================
# network.json
# ============================================================================

class NetworkNode(SlimModel):
    id: str
    type: NodeType = Field(alias="ty")
    full_name: str = Field(default="", alias="fn")
    is_source: Optional[bool] = Field(default=None, alias="src")


class NetworkEdge(SlimModel):
    source: str = Field(alias="s")
    target: str = Field(alias="t")
    sign: int = Field(alias="x", description="1 = activation, -1 = inhibition")
    edge_id: str = Field(default="", alias="eid")
    doi: DoiStr = Field(default="", alias="d")


class NetworkMetadata(FlashPMetadata):
    total_nodes: Optional[int] = None
    total_edges: Optional[int] = None
    source_nodes: Optional[int] = None
    source_percentage: Optional[float] = None


class NetworkFile(SlimModel):
    metadata: NetworkMetadata
    nodes: List[NetworkNode]
    edges: List[NetworkEdge]


# ============================================================================
# algebraic_equations.json
# ============================================================================

class AlgebraicParameters(SlimModel):
    """Load-bearing constants — kept READABLE (one block per file)."""
    epsilon: float = 0.1
    K: float = 10.0
    activator_floor: float = 0.01
    damping: float = 0.7
    direction_threshold: float = 0.05
    max_iterations: int = 100
    convergence_tolerance: float = 0.0001


class AlgebraicEquation(SlimModel):
    node: str = Field(alias="n")
    type: NodeType = Field(alias="ty")
    is_source: bool = Field(default=False, alias="src")
    activators: List[str] = Field(default_factory=list, alias="a")
    inhibitors: List[str] = Field(default_factory=list, alias="inh")
    formula: str = Field(alias="f")


class AlgebraicEquationsMetadata(FlashPMetadata):
    total_equations: Optional[int] = None


class AlgebraicEquationsFile(SlimModel):
    metadata: AlgebraicEquationsMetadata
    parameters: AlgebraicParameters = Field(default_factory=AlgebraicParameters)
    equations: List[AlgebraicEquation]


# ============================================================================
# ode_equations.json
# ============================================================================

class ODEEquation(SlimModel):
    node: str = Field(alias="n")
    activators: List[str] = Field(default_factory=list, alias="a")
    inhibitors: List[str] = Field(default_factory=list, alias="inh")
    formula: str = Field(alias="f")


class ODEEquationsMetadata(SlimModel):
    method: str = "ODE (Hill Functions)"
    K: float = 1.0
    n: int = 2
    direction_threshold: float = 0.05
    activator_floor: float = 0.01


class ODEEquationsFile(SlimModel):
    metadata: ODEEquationsMetadata = Field(default_factory=ODEEquationsMetadata)
    equations: List[ODEEquation]


# ============================================================================
# node_annotations.json  (degrees dropped — recomputable from edges)
# ============================================================================

class NodeAnnotation(SlimModel):
    node: str = Field(alias="n")
    full_name: str = Field(default="", alias="fn")
    type: NodeType = Field(alias="ty")
    description: str = Field(default="", alias="desc")
    is_source: bool = Field(default=False, alias="src")


class NodeAnnotationsMetadata(FlashPMetadata):
    total_nodes: Optional[int] = None


class NodeAnnotationsFile(SlimModel):
    metadata: NodeAnnotationsMetadata
    annotations: List[NodeAnnotation]
