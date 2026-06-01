import { Injectable } from '@nestjs/common';
import { PrismaService } from '../../prisma/prisma.service';

/** DTO de salida que consume el chatbot (coincide con su modelo `Order`). */
export interface BotOrderDto {
  id: string;
  cliente: string;
  telefono: string | null;
  estado: string;
  estado_embarque: string | null;
  estado_factura: string | null;
  total: number | null;
  moneda: string;
  fecha: string | null;
  fecha_entrega_estimada: string | null;
  lineas: { producto: string; cantidad: number; unidad: string }[];
  notas: string | null;
}

@Injectable()
export class BotService {
  constructor(private readonly prisma: PrismaService) {}

  // Incluye lo necesario para armar el DTO. Ajusta nombres de relación si difieren.
  private readonly include = {
    cliente: true,
    embarques: { orderBy: { fecha: 'desc' as const }, take: 1 },
    facturas: { orderBy: { fechaEmision: 'desc' as const }, take: 1 },
  };

  private toDto(contrato: any): BotOrderDto {
    const cliente = contrato.cliente ?? {};
    const embarque = contrato.embarques?.[0];
    const factura = contrato.facturas?.[0];
    return {
      id: contrato.folio,
      cliente: cliente.razonSocial ?? cliente.nombre ?? '',
      telefono: cliente.telefono ?? cliente.contactoTelefono ?? null,
      estado: contrato.estado,
      estado_embarque: embarque?.estado ?? null,
      estado_factura: factura?.estado ?? null,
      // montoTotal es BigInt en centavos -> pesos.
      total:
        contrato.montoTotal != null ? Number(contrato.montoTotal) / 100 : null,
      moneda: contrato.moneda ?? 'MXN',
      fecha: contrato.fechaContrato?.toISOString().slice(0, 10) ?? null,
      fecha_entrega_estimada:
        contrato.fechaEntregaEstimada?.toISOString().slice(0, 10) ?? null,
      lineas: [
        {
          producto: contrato.tipoGrano,
          cantidad: Number(contrato.toneladas),
          unidad: 'ton',
        },
      ],
      notas: null,
    };
  }

  async getOrderByFolio(folio: string): Promise<BotOrderDto | null> {
    const contrato = await this.prisma.contrato.findUnique({
      where: { folio },
      include: this.include,
    });
    return contrato ? this.toDto(contrato) : null;
  }

  async listOrdersByPhone(telefono: string): Promise<BotOrderDto[]> {
    const contratos = await this.prisma.contrato.findMany({
      where: {
        cliente: {
          OR: [{ telefono }, { contactoTelefono: telefono }],
        },
      },
      include: this.include,
      orderBy: { fechaContrato: 'desc' },
    });
    return contratos.map((c) => this.toDto(c));
  }
}
