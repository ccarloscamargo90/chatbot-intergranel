import { Injectable } from '@nestjs/common';
import { PrismaService } from '../../prisma/prisma.service';

/** Existencia que consume el chatbot (modelo `InventoryItem`). */
export interface BotInventoryItemDto {
  producto: string;
  stock_ton: number;
  umbral_ton: number;
  ubicacion: string | null;
  estado: string; // 'normal' | 'bajo_umbral'
}

@Injectable()
export class BotInventarioService {
  constructor(private readonly prisma: PrismaService) {}

  // Ajusta el modelo/campos de Prisma a tu esquema. Aquí se asume `Inventario`
  // con: producto, stockTon (Decimal), umbralTon (Decimal), ubicacion.
  private toDto(i: any): BotInventoryItemDto {
    const stock = Number(i.stockTon);
    const umbral = Number(i.umbralTon);
    return {
      producto: i.producto,
      stock_ton: stock,
      umbral_ton: umbral,
      ubicacion: i.ubicacion ?? null,
      estado: stock < umbral ? 'bajo_umbral' : 'normal',
    };
  }

  async getInventoryItem(producto: string): Promise<BotInventoryItemDto | null> {
    const item = await this.prisma.inventario.findFirst({
      where: { producto: { equals: producto, mode: 'insensitive' } },
    });
    return item ? this.toDto(item) : null;
  }

  async listInventory(): Promise<BotInventoryItemDto[]> {
    const items = await this.prisma.inventario.findMany({
      orderBy: { producto: 'asc' },
    });
    return items.map((i) => this.toDto(i));
  }
}
