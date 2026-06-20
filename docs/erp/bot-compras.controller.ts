import {
  Body,
  Controller,
  Get,
  NotFoundException,
  Param,
  Patch,
  Post,
  UseGuards,
} from '@nestjs/common';
import { ApiOperation, ApiSecurity, ApiTags } from '@nestjs/swagger';
import { IsNumber, IsPositive, IsString } from 'class-validator';
import { BotApiKeyGuard } from './bot-api-key.guard';
import {
  BotComprasService,
  BotPurchaseOrderDto,
  BotSupplierDto,
} from './bot-compras.service';
// Ajusta el import al decorador real de bypass del guard JWT global de tu repo.
import { Public } from '../../common/decorators/public.decorator';

class CrearOcDto {
  @IsString() proveedor: string;
  @IsString() producto: string;
  @IsNumber() @IsPositive() cantidad: number;
}

@ApiTags('bot')
@ApiSecurity('bot-api-key')
@Public()
@UseGuards(BotApiKeyGuard)
@Controller({ path: 'bot', version: '1' })
export class BotComprasController {
  constructor(private readonly compras: BotComprasService) {}

  @Get('oc/:folio')
  @ApiOperation({ summary: 'Detalle de una orden de compra por folio' })
  async getOc(@Param('folio') folio: string): Promise<BotPurchaseOrderDto> {
    const oc = await this.compras.getPurchaseOrder(folio);
    if (!oc) {
      throw new NotFoundException(`OC ${folio} no encontrada`);
    }
    return oc;
  }

  @Get('oc')
  @ApiOperation({ summary: 'Lista de OC pendientes de aprobación' })
  async listPending(): Promise<BotPurchaseOrderDto[]> {
    // El chatbot consulta con ?estado=pendiente; devolvemos las pendientes.
    return this.compras.listPendingPurchaseOrders();
  }

  @Post('oc')
  @ApiOperation({ summary: 'Crea una orden de compra a un proveedor' })
  async createOc(@Body() dto: CrearOcDto): Promise<BotPurchaseOrderDto> {
    return this.compras.createPurchaseOrder(
      dto.proveedor,
      dto.producto,
      dto.cantidad,
    );
  }

  @Patch('oc/:folio/aprobar')
  @ApiOperation({ summary: 'Aprueba una orden de compra' })
  async approveOc(@Param('folio') folio: string): Promise<BotPurchaseOrderDto> {
    const oc = await this.compras.approvePurchaseOrder(folio);
    if (!oc) {
      throw new NotFoundException(`OC ${folio} no encontrada`);
    }
    return oc;
  }

  @Get('proveedores')
  @ApiOperation({ summary: 'Lista de proveedores registrados' })
  async listSuppliers(): Promise<BotSupplierDto[]> {
    return this.compras.listSuppliers();
  }
}
