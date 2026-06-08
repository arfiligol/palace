// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

#include "surfacepostoperator.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <complex>
#include <map>
#include <set>
#include "fem/gridfunction.hpp"
#include "fem/integrator.hpp"
#include "linalg/vector.hpp"
#include "models/materialoperator.hpp"
#include "models/strattonchu.hpp"
#include "utils/communication.hpp"
#include "utils/geodata.hpp"
#include "utils/iodata.hpp"
#include "utils/prettyprint.hpp"
#include "utils/timer.hpp"

namespace palace
{

namespace
{

template <typename T>
mfem::Array<int> SetUpBoundaryProperties(const T &data,
                                         const mfem::Array<int> &bdr_attr_marker)
{
  mfem::Array<int> attr_list;
  attr_list.Reserve(static_cast<int>(data.attributes.size()));
  std::set<int> bdr_warn_list;
  for (auto attr : data.attributes)
  {
    // MFEM_VERIFY(attr > 0 && attr <= bdr_attr_max,
    //             "Boundary postprocessing attribute tags must be non-negative and "
    //             "correspond to attributes in the mesh!");
    // MFEM_VERIFY(bdr_attr_marker[attr - 1],
    //             "Unknown boundary postprocessing attribute " << attr << "!");
    if (attr <= 0 || attr > bdr_attr_marker.Size() || !bdr_attr_marker[attr - 1])
    {
      bdr_warn_list.insert(attr);
    }
    else
    {
      attr_list.Append(attr);
    }
  }
  if (!bdr_warn_list.empty())
  {
    Mpi::Print("\n");
    Mpi::Warning(
        "Unknown boundary postprocessing attributes!\nSolver will just ignore them!");
    utils::PrettyPrint(bdr_warn_list, "Boundary attribute list:");
    Mpi::Print("\n");
  }
  return attr_list;
}

void GetGlobalVertexIndices(const mfem::ParMesh &mesh, mfem::Array<HYPRE_BigInt> &gi)
{
  gi.SetSize(mesh.GetNV());
  if (mesh.Nonconforming())
  {
    gi = -1;
    const auto &ncmesh = *mesh.ncmesh;
    for (int n = 0; n < ncmesh.GetNumNodes(); n++)
    {
      const auto &node = ncmesh.GetNode(n);
      if (node.HasVertex())
      {
        const int v = node.vert_index;
        if (v >= 0 && v < mesh.GetNV())
        {
          gi[v] = n;
        }
      }
    }
    return;
  }
  mesh.GetGlobalVertexIndices(gi);
}

std::vector<SurfaceMaskEdge> GetInsetMaskEdges(const mfem::ParMesh &mesh,
                                               const mfem::Array<int> &attr_list,
                                               double margin)
{
  if (margin <= 0.0)
  {
    return {};
  }

  MFEM_VERIFY(mesh.Dimension() == 3 && mesh.SpaceDimension() == 3,
              "Inset interface dielectric masks are supported only for 3D "
              "boundary-surface postprocessing. 2D boundary-curve masks are not "
              "supported!");

  const int bdr_attr_max = mesh.bdr_attributes.Size() ? mesh.bdr_attributes.Max() : 0;
  const auto attr_marker = mesh::AttrToMarker(bdr_attr_max, attr_list);

  mfem::Array<HYPRE_BigInt> vertex_gi;
  GetGlobalVertexIndices(mesh, vertex_gi);

  std::vector<HYPRE_BigInt> local_keys;
  std::vector<double> local_coords;
  mfem::Array<int> edges, orientations, edge_vertices;
  for (int i = 0; i < mesh.GetNBE(); i++)
  {
    if (!attr_marker[mesh.GetBdrAttribute(i) - 1])
    {
      continue;
    }
    mesh.GetBdrElementEdges(i, edges, orientations);
    for (const auto edge : edges)
    {
      mesh.GetEdgeVertices(edge, edge_vertices);
      MFEM_VERIFY(edge_vertices.Size() == 2,
                  "Inset interface dielectric masks require boundary edges with two "
                  "vertices!");
      const HYPRE_BigInt gv0 = vertex_gi[edge_vertices[0]];
      const HYPRE_BigInt gv1 = vertex_gi[edge_vertices[1]];
      MFEM_VERIFY(gv0 >= 0 && gv1 >= 0,
                  "Failed to determine global vertex identifiers for inset interface "
                  "dielectric mask perimeter extraction!");
      const auto key0 = std::min(gv0, gv1);
      const auto key1 = std::max(gv0, gv1);
      local_keys.insert(local_keys.end(), {key0, key1});

      const auto *x0 = mesh.GetVertex(edge_vertices[0]);
      const auto *x1 = mesh.GetVertex(edge_vertices[1]);
      local_coords.insert(local_coords.end(), {x0[0], x0[1], x0[2], x1[0], x1[1], x1[2]});
    }
  }

  const auto comm = mesh.GetComm();
  const int comm_size = Mpi::Size(comm);
  const int local_key_count = static_cast<int>(local_keys.size());
  std::vector<int> key_recv_counts(comm_size);
  Mpi::Allgather(1, &local_key_count, key_recv_counts.data(), comm);
  std::vector<int> key_displs(comm_size);
  int global_key_count = 0;
  for (int i = 0; i < comm_size; i++)
  {
    key_displs[i] = global_key_count;
    global_key_count += key_recv_counts[i];
  }
  std::vector<HYPRE_BigInt> global_keys(global_key_count);
  Mpi::Allgatherv(local_key_count, local_keys.data(), global_keys.data(),
                  key_recv_counts.data(), key_displs.data(), comm);

  const int local_coord_count = static_cast<int>(local_coords.size());
  std::vector<int> coord_recv_counts(comm_size);
  Mpi::Allgather(1, &local_coord_count, coord_recv_counts.data(), comm);
  std::vector<int> coord_displs(comm_size);
  int global_coord_count = 0;
  for (int i = 0; i < comm_size; i++)
  {
    coord_displs[i] = global_coord_count;
    global_coord_count += coord_recv_counts[i];
  }
  std::vector<double> global_coords(global_coord_count);
  Mpi::Allgatherv(local_coord_count, local_coords.data(), global_coords.data(),
                  coord_recv_counts.data(), coord_displs.data(), comm);

  MFEM_VERIFY(global_keys.size() % 2 == 0 && global_coords.size() % 6 == 0 &&
                  global_keys.size() / 2 == global_coords.size() / 6,
              "Inconsistent inset interface dielectric mask perimeter data gathered "
              "across MPI ranks!");

  struct EdgeData
  {
    int count = 0;
    SurfaceMaskEdge edge;
  };

  std::map<std::pair<HYPRE_BigInt, HYPRE_BigInt>, EdgeData> edge_data;
  for (std::size_t i = 0; i < global_keys.size() / 2; i++)
  {
    const auto key = std::make_pair(global_keys[2 * i], global_keys[2 * i + 1]);
    auto &data = edge_data[key];
    data.count++;
    if (data.count == 1)
    {
      const auto j = 6 * i;
      data.edge = SurfaceMaskEdge{
          {global_coords[j], global_coords[j + 1], global_coords[j + 2]},
          {global_coords[j + 3], global_coords[j + 4], global_coords[j + 5]}};
    }
  }

  std::vector<SurfaceMaskEdge> mask_edges;
  for (const auto &[key, data] : edge_data)
  {
    if (data.count == 1)
    {
      mask_edges.push_back(data.edge);
    }
  }
  MFEM_VERIFY(!mask_edges.empty(),
              "Inset interface dielectric mask found no perimeter edges for the "
              "selected boundary attributes. Select an open 3D boundary-surface patch or "
              "set Margin to zero.");
  return mask_edges;
}

class InsetMaskCoefficient : public mfem::Coefficient
{
private:
  std::unique_ptr<mfem::Coefficient> coeff;
  std::vector<SurfaceMaskEdge> edges;
  double margin2;

public:
  InsetMaskCoefficient(std::unique_ptr<mfem::Coefficient> &&coeff,
                       std::vector<SurfaceMaskEdge> edges, double margin)
    : coeff(std::move(coeff)), edges(std::move(edges)), margin2(margin * margin)
  {
  }

  double Eval(mfem::ElementTransformation &T, const mfem::IntegrationPoint &ip) override
  {
    MFEM_VERIFY(T.GetSpaceDim() == 3,
                "Inset interface dielectric masks are supported only for 3D "
                "boundary-surface postprocessing!");

    mfem::Vector x(T.GetSpaceDim());
    T.Transform(ip, x);
    double d2_min = mfem::infinity();
    for (const auto &edge : edges)
    {
      const double ab0 = edge.b[0] - edge.a[0];
      const double ab1 = edge.b[1] - edge.a[1];
      const double ab2 = edge.b[2] - edge.a[2];
      const double ax0 = x[0] - edge.a[0];
      const double ax1 = x[1] - edge.a[1];
      const double ax2 = x[2] - edge.a[2];
      const double ab2_norm = ab0 * ab0 + ab1 * ab1 + ab2 * ab2;
      if (ab2_norm == 0.0)
      {
        continue;
      }
      const double t = std::clamp((ax0 * ab0 + ax1 * ab1 + ax2 * ab2) / ab2_norm, 0.0, 1.0);
      const double dx = ax0 - t * ab0;
      const double dy = ax1 - t * ab1;
      const double dz = ax2 - t * ab2;
      d2_min = std::min(d2_min, dx * dx + dy * dy + dz * dz);
    }
    if (d2_min < margin2)
    {
      return 0.0;
    }
    return coeff->Eval(T, ip);
  }
};

}  // namespace

SurfacePostOperator::SurfaceFluxData::SurfaceFluxData(
    const config::SurfaceFluxData &data, const mfem::ParMesh &mesh,
    const mfem::Array<int> &bdr_attr_marker)
{
  // Store boundary attributes for this postprocessing boundary.
  attr_list = SetUpBoundaryProperties(data, bdr_attr_marker);

  // Store the type of flux.
  switch (data.type)
  {
    case SurfaceFlux::ELECTRIC:
      type = SurfaceFlux::ELECTRIC;
      break;
    case SurfaceFlux::MAGNETIC:
      type = SurfaceFlux::MAGNETIC;
      break;
    case SurfaceFlux::POWER:
      type = SurfaceFlux::POWER;
      break;
  }

  // Store information about the global direction for orientation. Note the true boundary
  // normal is used in calculating the flux, this is just used to determine the sign.
  two_sided = data.two_sided;
  if (!two_sided)
  {
    center.SetSize(mesh.SpaceDimension());
    if (data.no_center)
    {
      // Compute the center as the bounding box centroid for all boundary elements making up
      // this postprocessing boundary.
      mfem::Vector bbmin, bbmax;
      mesh::GetAxisAlignedBoundingBox(
          mesh, mesh::AttrToMarker(bdr_attr_marker.Size(), attr_list), true, bbmin, bbmax);
      for (int d = 0; d < mesh.SpaceDimension(); d++)
      {
        center(d) = 0.5 * (bbmin(d) + bbmax(d));
      }
    }
    else
    {
      std::copy(data.center.begin(), data.center.end(), center.begin());
    }
  }
}

std::unique_ptr<mfem::Coefficient>
SurfacePostOperator::SurfaceFluxData::GetCoefficient(const mfem::ParGridFunction *E,
                                                     const mfem::ParGridFunction *B,
                                                     const MaterialOperator &mat_op) const
{
  switch (type)
  {
    case SurfaceFlux::ELECTRIC:
      return std::make_unique<
          RestrictedCoefficient<BdrSurfaceFluxCoefficient<SurfaceFlux::ELECTRIC>>>(
          attr_list, E, nullptr, mat_op, two_sided, center);
    case SurfaceFlux::MAGNETIC:
      return std::make_unique<
          RestrictedCoefficient<BdrSurfaceFluxCoefficient<SurfaceFlux::MAGNETIC>>>(
          attr_list, nullptr, B, mat_op, two_sided, center);
    case SurfaceFlux::POWER:
      return std::make_unique<
          RestrictedCoefficient<BdrSurfaceFluxCoefficient<SurfaceFlux::POWER>>>(
          attr_list, E, B, mat_op, two_sided, center);
  }
  return {};
}

SurfacePostOperator::InterfaceDielectricData::InterfaceDielectricData(
    const config::InterfaceDielectricData &data, const mfem::ParMesh &mesh,
    const mfem::Array<int> &bdr_attr_marker)
{
  // Store boundary attributes for this postprocessing boundary.
  attr_list = SetUpBoundaryProperties(data, bdr_attr_marker);

  // Calculate surface dielectric loss according to the formulas from J. Wenner et al.,
  // Surface loss simulations of superconducting coplanar waveguide resonators, Appl. Phys.
  // Lett. (2011). If only a general layer permittivity is specified and not any special
  // metal-air (MA), metal-substrate (MS), or substrate-air (SA) permittivity, compute the
  // numerator of the participation ratio according to the regular formula
  //                       p * E_elec = 1/2 t Re{∫ (ε E)ᴴ E_m dS} .
  switch (data.type)
  {
    case InterfaceDielectric::DEFAULT:
      type = InterfaceDielectric::DEFAULT;
      break;
    case InterfaceDielectric::MA:
      type = InterfaceDielectric::MA;
      break;
    case InterfaceDielectric::MS:
      type = InterfaceDielectric::MS;
      break;
    case InterfaceDielectric::SA:
      type = InterfaceDielectric::SA;
      break;
  }
  t = data.t;
  epsilon = data.epsilon_r;
  tandelta = data.tandelta;
  if (data.mask)
  {
    has_mask = true;
    mask_margin = data.mask->margin;
    MFEM_VERIFY(mesh.Dimension() == 3 && mesh.SpaceDimension() == 3,
                "Inset interface dielectric masks are supported only for 3D "
                "boundary-surface postprocessing. 2D boundary-curve masks are not "
                "supported!");
    mask_edges = GetInsetMaskEdges(mesh, attr_list, mask_margin);
  }
}

std::unique_ptr<mfem::Coefficient>
SurfacePostOperator::InterfaceDielectricData::GetCoefficient(
    const GridFunction &E, const MaterialOperator &mat_op) const
{
  switch (type)
  {
    case InterfaceDielectric::DEFAULT:
      return std::make_unique<RestrictedCoefficient<
          InterfaceDielectricCoefficient<InterfaceDielectric::DEFAULT>>>(
          attr_list, E, mat_op, t, epsilon);
    case InterfaceDielectric::MA:
      return std::make_unique<
          RestrictedCoefficient<InterfaceDielectricCoefficient<InterfaceDielectric::MA>>>(
          attr_list, E, mat_op, t, epsilon);
    case InterfaceDielectric::MS:
      return std::make_unique<
          RestrictedCoefficient<InterfaceDielectricCoefficient<InterfaceDielectric::MS>>>(
          attr_list, E, mat_op, t, epsilon);
    case InterfaceDielectric::SA:
      return std::make_unique<
          RestrictedCoefficient<InterfaceDielectricCoefficient<InterfaceDielectric::SA>>>(
          attr_list, E, mat_op, t, epsilon);
  }
  return {};  // For compiler warning
}

std::unique_ptr<mfem::Coefficient>
SurfacePostOperator::InterfaceDielectricData::GetMaskedCoefficient(
    const GridFunction &E, const MaterialOperator &mat_op) const
{
  MFEM_VERIFY(has_mask,
              "Masked interface dielectric coefficient requested without mask data!");
  auto coeff = GetCoefficient(E, mat_op);
  if (mask_margin <= 0.0)
  {
    return coeff;
  }
  return std::make_unique<InsetMaskCoefficient>(std::move(coeff), mask_edges, mask_margin);
}

SurfacePostOperator::FarFieldData::FarFieldData(const config::FarFieldPostData &data,
                                                const mfem::ParMesh &mesh,
                                                const mfem::Array<int> &bdr_attr_marker)
  : thetaphis(data.thetaphis)
{
  // Store boundary attributes for this postprocessing boundary.
  attr_list = SetUpBoundaryProperties(data, bdr_attr_marker);
}

SurfacePostOperator::SurfacePostOperator(const config::BoundaryPostData &postpro,
                                         ProblemType problem_type,
                                         const MaterialOperator &mat_op,
                                         mfem::ParFiniteElementSpace &h1_fespace,
                                         mfem::ParFiniteElementSpace &nd_fespace)
  : mat_op(mat_op), h1_fespace(h1_fespace), nd_fespace(nd_fespace)
{
  // Check that boundary attributes have been specified correctly.
  const auto &mesh = *h1_fespace.GetParMesh();
  int bdr_attr_max = mesh.bdr_attributes.Size() ? mesh.bdr_attributes.Max() : 0;
  mfem::Array<int> bdr_attr_marker;
  if (!postpro.flux.empty() || !postpro.dielectric.empty() || !postpro.farfield.empty())
  {
    bdr_attr_marker.SetSize(bdr_attr_max);
    bdr_attr_marker = 0;
    for (auto attr : mesh.bdr_attributes)
    {
      bdr_attr_marker[attr - 1] = 1;
    }
  }

  // Surface flux postprocessing.
  for (const auto &[idx, data] : postpro.flux)
  {
    MFEM_VERIFY(problem_type != ProblemType::ELECTROSTATIC ||
                    data.type == SurfaceFlux::ELECTRIC,
                "Magnetic field or power surface flux postprocessing are not available "
                "for electrostatic problems!");
    MFEM_VERIFY(problem_type != ProblemType::MAGNETOSTATIC ||
                    data.type == SurfaceFlux::MAGNETIC,
                "Electric field or power surface flux postprocessing are not available "
                "for magnetostatic problems!");
    flux_surfs.try_emplace(idx, data, *h1_fespace.GetParMesh(), bdr_attr_marker);
  }

  // Interface dielectric postprocessing.
  MFEM_VERIFY(postpro.dielectric.empty() || problem_type != ProblemType::MAGNETOSTATIC,
              "Interface dielectric loss postprocessing is not available for "
              "magnetostatic problems!");
  for (const auto &[idx, data] : postpro.dielectric)
  {
    eps_surfs.try_emplace(idx, data, *h1_fespace.GetParMesh(), bdr_attr_marker);
  }

  // FarField postprocessing.
  MFEM_VERIFY(postpro.farfield.empty() || problem_type == ProblemType::DRIVEN ||
                  problem_type == ProblemType::EIGENMODE,
              "Far-field extraction is only available for driven and eigenmode problems!");

  // Check that we don't have anisotropic materials.
  if (!postpro.farfield.empty())
  {
    const auto &mesh = *nd_fespace.GetParMesh();
    int bdr_attr_max = mesh.bdr_attributes.Size() ? mesh.bdr_attributes.Max() : 0;
    mfem::Array<int> bdr_attr_marker =
        mesh::AttrToMarker(bdr_attr_max, postpro.farfield.attributes);

    std::set<int> domain_attrs;

    for (int i = 0; i < mesh.GetNBE(); i++)
    {
      if (bdr_attr_marker[mesh.GetBdrAttribute(i) - 1])
      {
        int elem_id, _face_id;
        mesh.GetBdrElementAdjacentElement(i, elem_id, _face_id);
        if (elem_id >= 0)
        {
          domain_attrs.insert(mesh.GetAttribute(elem_id));
        }
      }
    }

    for (int attr : domain_attrs)
    {
      MFEM_VERIFY(mat_op.IsIsotropic(attr),
                  "FarField requires isotropic materials, but attribute " +
                      std::to_string(attr) + " is not.");
    }
  }

  farfield = FarFieldData(postpro.farfield, *nd_fespace.GetParMesh(), bdr_attr_marker);
}

SurfacePostOperator::SurfacePostOperator(const IoData &iodata,
                                         const MaterialOperator &mat_op,
                                         mfem::ParFiniteElementSpace &h1_fespace,
                                         mfem::ParFiniteElementSpace &nd_fespace)
  : SurfacePostOperator(iodata.boundaries.postpro, iodata.problem.type, mat_op, h1_fespace,
                        nd_fespace)
{
}

std::complex<double> SurfacePostOperator::GetSurfaceFlux(int idx, const GridFunction *E,
                                                         const GridFunction *B) const
{
  // For complex-valued fields, output the separate real and imaginary parts for the time-
  // harmonic quantity. For power flux (Poynting vector), output only the stationary real
  // part and not the part which has double the frequency.
  auto it = flux_surfs.find(idx);
  MFEM_VERIFY(it != flux_surfs.end(),
              "Unknown surface flux postprocessing index requested!");
  const bool has_imag = (E) ? E->HasImag() : B->HasImag();
  const auto &mesh = *h1_fespace.GetParMesh();
  int bdr_attr_max = mesh.bdr_attributes.Size() ? mesh.bdr_attributes.Max() : 0;
  mfem::Array<int> attr_marker = mesh::AttrToMarker(bdr_attr_max, it->second.attr_list);
  auto f =
      it->second.GetCoefficient(E ? &E->Real() : nullptr, B ? &B->Real() : nullptr, mat_op);
  std::complex<double> dot(GetLocalSurfaceIntegral(*f, attr_marker), 0.0);
  if (has_imag)
  {
    f = it->second.GetCoefficient(E ? &E->Imag() : nullptr, B ? &B->Imag() : nullptr,
                                  mat_op);
    double doti = GetLocalSurfaceIntegral(*f, attr_marker);
    if (it->second.type == SurfaceFlux::POWER)
    {
      dot += doti;
    }
    else
    {
      dot.imag(doti);
    }
  }
  Mpi::GlobalSum(1, &dot, (E) ? E->GetComm() : B->GetComm());
  return dot;
}

bool SurfacePostOperator::HasMaskedInterfaceDielectrics() const
{
  return std::any_of(eps_surfs.begin(), eps_surfs.end(),
                     [](const auto &surf) { return surf.second.HasMask(); });
}

std::vector<int> SurfacePostOperator::GetMaskedInterfaceIndices() const
{
  std::vector<int> indices;
  for (const auto &[idx, data] : eps_surfs)
  {
    if (data.HasMask())
    {
      indices.push_back(idx);
    }
  }
  return indices;
}

bool SurfacePostOperator::HasInterfaceMask(int idx) const
{
  auto it = eps_surfs.find(idx);
  MFEM_VERIFY(it != eps_surfs.end(),
              "Unknown interface dielectric postprocessing index requested!");
  return it->second.HasMask();
}

double SurfacePostOperator::GetInterfaceLossTangent(int idx) const
{
  auto it = eps_surfs.find(idx);
  MFEM_VERIFY(it != eps_surfs.end(),
              "Unknown interface dielectric postprocessing index requested!");
  return it->second.tandelta;
}

double SurfacePostOperator::GetInterfaceElectricFieldEnergy(int idx,
                                                            const GridFunction &E) const
{
  auto it = eps_surfs.find(idx);
  MFEM_VERIFY(it != eps_surfs.end(),
              "Unknown interface dielectric postprocessing index requested!");
  const auto &mesh = *h1_fespace.GetParMesh();
  int bdr_attr_max = mesh.bdr_attributes.Size() ? mesh.bdr_attributes.Max() : 0;
  mfem::Array<int> attr_marker = mesh::AttrToMarker(bdr_attr_max, it->second.attr_list);
  auto f = it->second.GetCoefficient(E, mat_op);
  double dot = GetLocalSurfaceIntegral(*f, attr_marker);
  Mpi::GlobalSum(1, &dot, E.GetComm());
  return dot;
}

double
SurfacePostOperator::GetMaskedInterfaceElectricFieldEnergy(int idx,
                                                           const GridFunction &E) const
{
  auto it = eps_surfs.find(idx);
  MFEM_VERIFY(it != eps_surfs.end(),
              "Unknown interface dielectric postprocessing index requested!");
  MFEM_VERIFY(it->second.HasMask(),
              "Masked interface dielectric energy requested without mask data!");
  const auto &mesh = *h1_fespace.GetParMesh();
  int bdr_attr_max = mesh.bdr_attributes.Size() ? mesh.bdr_attributes.Max() : 0;
  mfem::Array<int> attr_marker = mesh::AttrToMarker(bdr_attr_max, it->second.attr_list);
  auto f = it->second.GetMaskedCoefficient(E, mat_op);
  double dot = GetLocalSurfaceIntegral(*f, attr_marker);
  Mpi::GlobalSum(1, &dot, E.GetComm());
  return dot;
}

double
SurfacePostOperator::GetLocalSurfaceIntegral(mfem::Coefficient &f,
                                             const mfem::Array<int> &attr_marker) const
{
  // Integrate the coefficient over the boundary attributes making up this surface index.
  mfem::LinearForm s(&h1_fespace);
  s.AddBoundaryIntegrator(new BoundaryLFIntegrator(f),
                          const_cast<mfem::Array<int> &>(attr_marker));
  s.UseFastAssembly(false);
  s.UseDevice(false);
  s.Assemble();
  s.UseDevice(true);
  return linalg::LocalSum(s);
}

std::vector<std::array<std::complex<double>, 3>> SurfacePostOperator::GetFarFieldrE(
    const std::vector<std::pair<double, double>> &theta_phi_pairs, const GridFunction &E,
    const GridFunction &B, double omega_re, double omega_im) const
{
  if (theta_phi_pairs.empty())
    return {};
  MFEM_VERIFY(nd_fespace.GetParMesh()->SpaceDimension() == 3,
              "Far-field computation is only available for 3D simulations!");
  BlockTimer bt0(Timer::POSTPRO_FARFIELD);

  // Compute target unit vectors from the given theta and phis.
  std::vector<std::array<double, 3>> r_naughts;
  r_naughts.reserve(theta_phi_pairs.size());

  r_naughts.reserve(theta_phi_pairs.size());
  for (const auto &[theta, phi] : theta_phi_pairs)
  {
    r_naughts.emplace_back(std::array<double, 3>{
        std::sin(theta) * std::cos(phi), std::sin(theta) * std::sin(phi), std::cos(theta)});
  }

  const auto &mesh = *nd_fespace.GetParMesh();
  int bdr_attr_max = mesh.bdr_attributes.Size() ? mesh.bdr_attributes.Max() : 0;
  mfem::Array<int> attr_marker = mesh::AttrToMarker(bdr_attr_max, farfield.attr_list);

  // Integrate. Each MPI process computes its contribution and we will reduce
  // everything at the end. We make them std::vector<std::array<double, 3>>
  // because we want a very simple memory layout so that we can reduce
  // everything with two MPI calls.
  std::vector<std::array<double, 3>> integrals_r(theta_phi_pairs.size());
  std::vector<std::array<double, 3>> integrals_i(theta_phi_pairs.size());

  for (int i = 0; i < mesh.GetNBE(); i++)
  {
    if (!attr_marker[mesh.GetBdrAttribute(i) - 1])
      continue;

    auto *T = const_cast<mfem::ParMesh &>(mesh).GetBdrElementTransformation(i);
    const auto *fe = nd_fespace.GetBE(i);
    const auto *ir =
        &mfem::IntRules.Get(fe->GetGeomType(), fem::DefaultIntegrationOrder::Get(*T));

    AddStrattonChuIntegrandAtElement(E, B, mat_op, omega_re, omega_im, r_naughts, *T, *ir,
                                     integrals_r, integrals_i);
  }

  double *data_r_ptr = integrals_r.data()->data();
  double *data_i_ptr = integrals_i.data()->data();
  std::size_t total_elements = integrals_r.size() * 3;
  Mpi::GlobalSum(total_elements, data_i_ptr, E.GetComm());
  Mpi::GlobalSum(total_elements, data_r_ptr, E.GetComm());

  // Finally, we apply cross product to reduced integrals and package the result
  // in a neatly accessible vector of arrays of complex numbers.
  std::vector<std::array<std::complex<double>, 3>> result(theta_phi_pairs.size());
  StaticVector<3> tmp_r, tmp_i;
  for (std::size_t k = 0; k < theta_phi_pairs.size(); k++)
  {
    linalg::Cross3(r_naughts[k], integrals_r[k], tmp_r);
    linalg::Cross3(r_naughts[k], integrals_i[k], tmp_i);
    for (std::size_t d = 0; d < 3; d++)
    {
      result[k][d] = std::complex<double>{tmp_r[d], tmp_i[d]};
    }
  }
  return result;
}

}  // namespace palace
