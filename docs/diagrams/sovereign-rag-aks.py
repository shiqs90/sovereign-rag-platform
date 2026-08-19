"""Sovereign multi-tenant RAG on AKS — how one tenant's question stays inside its own corpus.

A RUNTIME diagram. Provisioning is absent on purpose: Terraform, HCP state, the image build,
the corpus extraction. They create the system; they take no part in a query. The two
terraform-time decisions that DO decide whether this works at all — Calico as the network
policy engine, and gpu_driver = "None" so the Operator owns the driver stack — are noted on
the data plane and the Operator, where their effect is felt. Rationale in terraform/aks.tf.

Numbered edges follow tenant-a's question end to end. tenant-b's path is identical and drawn
unnumbered — that symmetry is the point. Dotted edges are standing background. RED edges are
the calls that MUST fail; scripts/verify-eval-isolation.sh asserts each of them.

STRUCTURE is containment by NAMESPACE, not by node pool, because the namespace is the
isolation boundary and the thesis is isolation. The node pool a workload lands on is written
into its label instead (`apps pool`, `GPU pool`) — the two axes are orthogonal and graphviz
clusters cannot overlap.

COLOUR of a BOX encodes ownership:
    grey = outside the sovereign boundary   blue = Azure-managed
    green = the AKS data plane              white = namespaces you own

COLOUR of an ARROW encodes whose request it is, and each tenant's arrows match its own
namespace border, so a path can be followed by colour alone:
    teal = tenant-a      purple = tenant-b      black = shared by both
    red  = a call that MUST fail (proof, not plumbing)

The interesting shape is teal and purple arriving at the SAME tei / qdrant / vllm nodes and
still never mixing: shared compute, non-shared data.

ICON = what the thing is.  LABEL = how it is deployed + which pool it lands on.

LABELS ARE TWO SHORT LINES, MAX. graphviz draws a fixed-size icon with the label beneath at
full text width, so long labels overrun their neighbours and the page turns to soup. Every
number, CIDR, flag and rationale belongs in README.md, not on the canvas.

Render:  python3 docs/diagrams/sovereign-rag-aks.py  ->  docs/diagrams/sovereign-rag-aks.png
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.azure.compute import ACR, AKS
from diagrams.azure.network import LoadBalancers
from diagrams.k8s.compute import DaemonSet
from diagrams.k8s.podconfig import Secret
from diagrams.onprem.certificates import CertManager
from diagrams.onprem.client import Users
from diagrams.onprem.compute import Server
from diagrams.onprem.database import Qdrant
from diagrams.onprem.network import Nginx
from diagrams.programming.framework import Fastapi
from diagrams.programming.language import Python

graph_attr = {
    "fontsize": "16",
    "bgcolor": "white",
    "pad": "0.6",
    "nodesep": "1.0",
    "ranksep": "1.6",
    "splines": "spline",
}
node_attr = {"fontsize": "13"}
edge_attr = {"fontsize": "12"}

OUTSIDE = {"bgcolor": "#F3F3F1", "pencolor": "#9AA0A6", "fontsize": "15"}
AZURE = {"bgcolor": "#E8F0FE", "pencolor": "#0078D4", "fontsize": "16"}
AZURE_MANAGED = {"bgcolor": "#D2E3FC", "pencolor": "#004578", "style": "rounded,dashed", "fontsize": "14"}
CLUSTER = {"bgcolor": "#E6F4EA", "pencolor": "#137333", "penwidth": "2", "fontsize": "16"}
PLATFORM = {"bgcolor": "#FFFFFF", "pencolor": "#5F6368", "style": "rounded,dashed", "fontsize": "14"}
# One colour per tenant, used for BOTH the namespace border and every arrow on that
# tenant's path. Teal / purple / red are distinguishable from each other and from the
# grey-green-blue fills; orange was rejected because it reads as red at arrow width.
A = "#00838F"
B = "#7B1FA2"
DENIED = "#C5221F"
BACKGROUND = "#3C4043"

TENANT_A = {"bgcolor": "#FFFFFF", "pencolor": A, "style": "rounded", "penwidth": "2", "fontsize": "14"}
TENANT_B = {"bgcolor": "#FFFFFF", "pencolor": B, "style": "rounded", "penwidth": "2", "fontsize": "14"}

with Diagram(
    "Sovereign multi-tenant RAG on AKS",
    filename="docs/diagrams/sovereign-rag-aks",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):
    with Cluster("OUTSIDE THE SOVEREIGN BOUNDARY", graph_attr=OUTSIDE):
        analyst_a = Users("tenant-a analyst\nBearer key-a")
        analyst_b = Users("tenant-b analyst\nBearer key-b")
        ingest = Python("ingest.py\ncorpus, one-off")
        hf = Server("HuggingFace Hub\nweights, first boot")

    with Cluster("Azure · australiacentral · rg-sovereign-rag", graph_attr=AZURE):
        with Cluster("AKS CONTROL PLANE — Azure-owned", graph_attr=AZURE_MANAGED):
            control_plane = AKS("sovereign-rag\nFree tier")

        acr = ACR("ACR Basic\nrag-service:v3")
        lb = LoadBalancers("Azure LB\nMC_ RG, AKS-owned")

        with Cluster("AKS DATA PLANE — 3 node pools · Calico enforcing", graph_attr=CLUSTER):
            with Cluster("ns ingress-nginx / cert-manager", graph_attr=PLATFORM):
                nginx = Nginx("ingress-nginx\nsystem pool")
                certs = CertManager("cert-manager\nself-signed CA")

            with Cluster("ns tenant-a", graph_attr=TENANT_A):
                rag_a = Fastapi("rag · Deployment\napps pool")
                key_a = Secret("rag-api-keys\nRole: this Secret only")

            with Cluster("ns tenant-b", graph_attr=TENANT_B):
                rag_b = Fastapi("rag · Deployment\napps pool")
                key_b = Secret("rag-api-keys\nRole: this Secret only")

            with Cluster("ns rag-platform — shared, ClusterIP only", graph_attr=PLATFORM):
                tei = Server("TEI · bge-m3\napps pool · CPU")
                qdrant = Qdrant("Qdrant · StatefulSet\ncollection per tenant")
                vllm = Server("vLLM · Qwen2.5-7B-AWQ\nGPU pool · 1x T4")

            with Cluster("ns gpu-operator", graph_attr=PLATFORM):
                gpu_operator = DaemonSet("GPU Operator\ndriver · toolkit · plugin")

    # ---- tenant-a's question, in order -------------------------------------
    # `teal` / `purple` below are just (colour, fontcolor, penwidth) applied to every edge
    # on that tenant's path — the label carries the step, the colour carries the identity.
    teal = {"color": A, "fontcolor": A, "penwidth": "2"}
    purple = {"color": B, "fontcolor": B, "penwidth": "2"}

    # Background edges are dotted so they read as subordinate to the request path — but
    # graphviz draws `dotted` at penwidth 1 as sparse hairline dots that all but vanish
    # next to a 2px coloured arrow. Thicker dots in a definite dark grey keep the
    # hierarchy (dotted still reads as "standing background") while staying legible.
    # Not switched to `dashed`: dashed already means the key/token edges.
    bg = {"style": "dotted", "penwidth": "2.2", "color": BACKGROUND, "fontcolor": BACKGROUND}

    analyst_a >> Edge(label="1 · HTTPS\nrag-a.<ip>.nip.io", **teal) >> lb
    # The LB and the nginx controller are the ONE stretch both tenants share on the way in,
    # so it stays black. Separation begins at the Host header, not before it.
    lb >> Edge(label="2 · TLS terminate\nshared by both tenants") >> nginx
    nginx >> Edge(label="3 · route by Host", **teal) >> rag_a
    key_a >> Edge(label="4 · key -> tenant\n(never a header)", style="dashed", **teal) >> rag_a
    rag_a >> Edge(label="5 · POST /embed", **teal) >> tei
    rag_a >> Edge(label="6 · search, filter\ntenant_id inside retriever", **teal) >> qdrant
    rag_a >> Edge(label="7 · /v1/completions\n8 · marker -> citation", **teal) >> vllm

    # ---- tenant-b: same shape, other collection, no shared state -----------
    analyst_b >> Edge(label="rag-b.<ip>.nip.io", **purple) >> lb
    nginx >> Edge(**purple) >> rag_b
    key_b >> Edge(style="dashed", **purple) >> rag_b
    rag_b >> Edge(**purple) >> tei
    rag_b >> Edge(label="collection tenant-b", **purple) >> qdrant
    rag_b >> Edge(**purple) >> vllm

    # ---- the denials the eval suite asserts ---------------------------------
    # Network: Calico drops this in the data plane, pod to pod. Nothing above sees it.
    rag_a >> Edge(label="NetworkPolicy\ndefault-deny egress", color=DENIED, style="dashed", fontcolor=DENIED) >> rag_b

    # Control plane: the SA token is a REAL call and reaches the API server; the API server
    # is what refuses it. Drawing this as one pod-to-Secret line would put the enforcement
    # in the wrong place — a Role is evaluated by the API server, never by the caller.
    rag_a >> Edge(label="SA rag-sa token", style="dashed", **teal) >> control_plane
    control_plane >> Edge(label="RBAC: no\n(Role scopes to own Secret)", color=DENIED, style="dashed", fontcolor=DENIED) >> key_b

    # ---- standing background, not part of the request ----------------------
    acr >> Edge(label="pull · kubelet identity", **bg) >> rag_a
    acr >> Edge(**bg) >> rag_b
    hf >> Edge(label="weights", **bg) >> vllm
    hf >> Edge(**bg) >> tei
    gpu_operator >> Edge(label="advertises\nnvidia.com/gpu: 1", **bg) >> vllm
    certs >> Edge(label="per-tenant TLS Secret", **bg) >> nginx
    ingest >> Edge(label="POST /ingest\nonce per corpus", **{**bg, "color": A, "fontcolor": A}) >> rag_a
    ingest >> Edge(**{**bg, "color": B}) >> rag_b
