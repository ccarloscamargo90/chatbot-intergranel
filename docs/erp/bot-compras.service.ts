import { Injectable } from '@nestjs/common';
import { PrismaService } from '../../prisma/prisma.service';

/** Orden de compra que consume el chatbot (modelo `PurchaseOrder`). */
export interface BotPurchaseOrderDto {
  id: string;
  proveedor: string;
  producto: string;
  cantidad: number;
  unidad: string;
  total: number | null;
  moneda: string;
  estado: string;
  fecha: string | null;
  fecha_entrega_estimada: string | null;
}

/** Proveedor que consume el chatbot (modelo `Supplier`). */
export interface BotSupplierDto {
  id: string;
  nombre: string;
  productos: string[];
  contacto: string | null;
}

@Injectable()
export class BotComprasService {
  constructor(private readonly prisma: PrismaService) {}

  private readonly include = { proveedor: true };

  // Ajusta nombres de modelo/campos de Prisma a tu esquema. Aquí se asume un
  // modelo `OrdenCompra` con: folio, proveedor (relación), producto, toneladas,
  // montoTotal (BigInt en centavos), moneda, estado, fechaOc, fechaEntregaEstimada.
  private toOcDto(oc: any): BotPurchaseOrderDto {
    return {
      id: oc.folio,
      proveedor: oc.proveedor?.razonSocial ?? oc.proveedor?.nombre ?? '',
      producto: oc.producto ?? oc.tipoGrano,
      cantidad: Number(oc.toneladas),
      unidad: 'ton',
      total: oc.montoTotal != null ? Number(oc.montoTotal) / 100 : null,
      moneda: oc.moneda ?? 'MXN',
      estado: oc.estado,
      fecha: oc.fechaOc?.toISOString().slice(0, 10) ?? null,
      fecha_entrega_estimada:
        oc.fechaEntregaEstimada?.toISOString().slice(0, 10) ?? null,
    };
  }

  async getPurchaseOrder(folio: string): Promise<BotPurchaseOrderDto | null> {
    const oc = await this.prisma.ordenCompra.findUnique({
      where: { folio },
      include: this.include,
    });
    return oc ? this.toOcDto(oc) : null;
  }

  async listPendingPurchaseOrders(): Promise<BotPurchaseOrderDto[]> {
    const ocs = await this.prisma.ordenCompra.findMany({
      where: { estado: 'pendiente' },
      include: this.include,
      orderBy: { fechaOc: 'desc' },
    });
    return ocs.map((oc) => this.toOcDto(oc));
  }

  async createPurchaseOrder(
    proveedor: string,
    producto: string,
    cantidad: number,
  ): Promise<BotPurchaseOrderDto> {
    // Resuelve el proveedor por nombre/razón social (créalo o ajústalo a tu flujo).
    const prov = await this.prisma.proveedor.findFirst({
      where: {
        OR: [
          { razonSocial: { equals: proveedor, mode: 'insensitive' } },
          { nombre: { equals: proveedor, mode: 'insensitive' } },
        ],
      },
    });
    const oc = await this.prisma.ordenCompra.create({
      data: {
        proveedorId: prov?.id,
        producto,
        toneladas: cantidad,
        estado: 'pendiente',
        fechaOc: new Date(),
      },
      include: this.include,
    });
    return this.toOcDto(oc);
  }

  async approvePurchaseOrder(
    folio: string,
  ): Promise<BotPurchaseOrderDto | null> {
    const existente = await this.prisma.ordenCompra.findUnique({
      where: { folio },
    });
    if (!existente) {
      return null;
    }
    const oc = await this.prisma.ordenCompra.update({
      where: { folio },
      data: { estado: 'aprobada' },
      include: this.include,
    });
    return this.toOcDto(oc);
  }

  async listSuppliers(): Promise<BotSupplierDto[]> {
    const proveedores = await this.prisma.proveedor.findMany({
      orderBy: { razonSocial: 'asc' },
    });
    return proveedores.map((p: any) => ({
      id: p.clave ?? String(p.id),
      nombre: p.razonSocial ?? p.nombre ?? '',
      productos: p.productos ?? [],
      contacto: p.contacto ?? p.contactoEmail ?? null,
    }));
  }
}
